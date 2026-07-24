"""Upload-hardening guards (issues #66 + #53): SSRF guard + zip decompression-bomb cap + .epub.

Everything here is NETWORK-FREE: literal-IP URLs are judged by ``getaddrinfo`` locally (no DNS
query for numeric hosts), hostname resolution is monkeypatched at the ``_resolve_host`` seam, and
the wire itself is monkeypatched at the ``_open_connection`` seam — a test that would ever open a
real socket also patches ``_open_connection`` to fail loudly. The zip-bomb fixtures are tiny
archives judged against a small explicit cap: the declared-size pre-check rejects honest bombs
without decompressing anything, and the under-declared fixtures really decompress only a few MiB
before the actual-size pass aborts at the (kilobyte/MiB) cap — no huge bytes are ever materialized.
"""

from __future__ import annotations

import io
import struct
import zipfile
import zlib

import pytest

from agora_kb.ingest.extractors import (
    ExtractorError,
    extract,
    extract_markitdown,
    extract_office,
    extract_url,
)
from agora_kb.ingest.extractors import url as url_mod
from agora_kb.ingest.extractors.office import (
    _DEFAULT_MAX_UNCOMPRESSED_BYTES,
    _guard_zip_bomb,
)

_HTML = (
    "<html><head><title>Agora Test Page</title></head>"
    "<body><article><h1>Agora</h1>"
    "<p>This is a real paragraph with enough words for trafilatura to recognize it as the "
    "main content of the page and extract it cleanly.</p></article></body></html>"
)


# --- shared network fakes -----------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None):
        self.status = status
        self._body = body
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name.lower(), default)

    def read(self, amt: int | None = None) -> bytes:
        return self._body if amt is None else self._body[:amt]


class _FakeConnection:
    """One scripted response per opened connection; records the request it saw."""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.requested: tuple[str, str] | None = None

    def request(self, method: str, path: str, headers: dict[str, str] | None = None) -> None:
        self.requested = (method, path)

    def getresponse(self) -> _FakeResponse:
        return self._response

    def close(self) -> None:
        pass


def _no_connect(*args: object, **kwargs: object) -> None:
    raise AssertionError("the SSRF guard must reject BEFORE any connection is opened")


def _script_connections(
    monkeypatch: pytest.MonkeyPatch, responses: list[_FakeResponse]
) -> list[tuple[str, str, str, int]]:
    """Patch _open_connection to hand out ``responses`` in order; return the (scheme, host, ip,
    port) tuples of every opened connection."""
    opened: list[tuple[str, str, str, int]] = []

    def fake_open(scheme: str, host: str, ip: str, port: int, *, timeout: float) -> _FakeConnection:
        opened.append((scheme, host, ip, port))
        return _FakeConnection(responses.pop(0))

    monkeypatch.setattr(url_mod, "_open_connection", fake_open)
    return opened


# --- (a) SSRF guard: scheme allowlist -----------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/pub/x",
        "gopher://example.com/1",
        "javascript:alert(1)",
        "//example.com/schemeless",
    ],
)
def test_ssrf_rejects_non_http_schemes(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("trafilatura")
    monkeypatch.setattr(url_mod, "_open_connection", _no_connect)
    with pytest.raises(ExtractorError, match="not allowed|invalid URL"):
        extract_url(url)


# --- (a) SSRF guard: private / loopback / link-local / metadata literals ------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/",  # RFC1918 10/8
        "http://172.16.0.5/x",  # RFC1918 172.16/12
        "http://192.168.1.1/router",  # RFC1918 192.168/16
        "http://127.0.0.1:8080/",  # loopback
        "http://169.254.169.254/latest/meta-data/",  # link-local incl. cloud metadata
        "http://0.0.0.0/",  # unspecified
        "http://[::1]/",  # IPv6 loopback
        "http://[fe80::1]/",  # IPv6 link-local
        "http://[fc00::1]/",  # IPv6 unique-local fc00::/7
        "http://[::]/",  # IPv6 unspecified
        "http://[::ffff:192.168.0.1]/",  # IPv4-mapped private must not slip through
        "https://10.0.0.1/tls-too",  # guard applies to https alike
    ],
)
def test_ssrf_rejects_non_public_ip_literals(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("trafilatura")
    monkeypatch.setattr(url_mod, "_open_connection", _no_connect)
    with pytest.raises(ExtractorError, match="SSRF guard"):
        extract_url(url)


def test_ssrf_rejects_hostname_resolving_to_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public-looking hostname whose DNS answer is private is blocked (resolve-then-validate)."""
    pytest.importorskip("trafilatura")
    monkeypatch.setattr(url_mod, "_resolve_host", lambda host, port: ["192.168.7.7"])
    monkeypatch.setattr(url_mod, "_open_connection", _no_connect)
    with pytest.raises(ExtractorError, match="private address 192.168.7.7"):
        extract_url("http://internal.corp.example/wiki")


def test_ssrf_rejects_when_any_resolved_ip_is_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """ALL resolved addresses must be public — one private A record fails the whole set (closed)."""
    pytest.importorskip("trafilatura")
    monkeypatch.setattr(url_mod, "_resolve_host", lambda host, port: ["93.184.216.34", "10.9.9.9"])
    monkeypatch.setattr(url_mod, "_open_connection", _no_connect)
    with pytest.raises(ExtractorError, match="SSRF guard"):
        extract_url("http://dual-homed.example/")


# --- (a) SSRF guard: public fetch succeeds (mocked wire) ----------------------------------------
def test_public_url_fetch_and_extract_via_mocked_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public-resolving host fetches over the PINNED connection and extracts real content."""
    pytest.importorskip("trafilatura")
    monkeypatch.setattr(url_mod, "_resolve_host", lambda host, port: ["93.184.216.34"])
    opened = _script_connections(monkeypatch, [_FakeResponse(200, _HTML.encode("utf-8"))])

    doc = extract_url("https://example.com/agora")
    assert doc.extractor == "url"
    assert doc.source_url == "https://example.com/agora"
    assert "main content" in doc.markdown
    # The connection was pinned to the VALIDATED ip while keeping the original hostname.
    assert opened == [("https", "example.com", "93.184.216.34", 443)]


def test_redirect_to_private_ip_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public first hop 302-ing to the metadata endpoint is re-validated and rejected."""
    pytest.importorskip("trafilatura")

    def fake_resolve(host: str, port: int) -> list[str]:
        table = {
            "pub.example": ["93.184.216.34"],
            "169.254.169.254": ["169.254.169.254"],
        }
        return table[host]

    monkeypatch.setattr(url_mod, "_resolve_host", fake_resolve)
    opened = _script_connections(
        monkeypatch,
        [_FakeResponse(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})],
    )

    with pytest.raises(ExtractorError, match="link-local address 169.254.169.254"):
        extract_url("http://pub.example/article")
    # Only the FIRST (public) hop ever got a connection; the private hop was blocked pre-connect.
    assert opened == [("http", "pub.example", "93.184.216.34", 80)]


def test_redirect_loop_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("trafilatura")
    monkeypatch.setattr(url_mod, "_resolve_host", lambda host, port: ["93.184.216.34"])
    bounce = _FakeResponse(301, headers={"Location": "http://pub.example/loop"})
    _script_connections(monkeypatch, [bounce] * 10)
    with pytest.raises(ExtractorError, match="too many redirects"):
        extract_url("http://pub.example/loop")


def test_non_200_status_raises_extractor_error(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("trafilatura")
    monkeypatch.setattr(url_mod, "_resolve_host", lambda host, port: ["93.184.216.34"])
    _script_connections(monkeypatch, [_FakeResponse(404)])
    with pytest.raises(ExtractorError, match="HTTP 404"):
        extract_url("http://pub.example/gone")


def test_oversize_body_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("trafilatura")
    monkeypatch.setattr(url_mod, "_resolve_host", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(url_mod, "_MAX_FETCH_BYTES", 10)
    _script_connections(monkeypatch, [_FakeResponse(200, b"x" * 11)])
    with pytest.raises(ExtractorError, match="too large"):
        extract_url("http://pub.example/huge")


# --- (a) the allow_private opt-out (local CLI callers; the web face never sets it) --------------
def test_allow_private_opt_in_fetches_internal_url(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("trafilatura")
    opened = _script_connections(monkeypatch, [_FakeResponse(200, _HTML.encode("utf-8"))])

    doc = extract_url("http://127.0.0.1:8080/wiki", allow_private=True)
    assert "main content" in doc.markdown
    assert opened == [("http", "127.0.0.1", "127.0.0.1", 8080)]


def test_allow_private_default_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("trafilatura")
    monkeypatch.setattr(url_mod, "_open_connection", _no_connect)
    with pytest.raises(ExtractorError, match="SSRF guard"):
        extract_url("http://127.0.0.1:8080/wiki")


def test_dispatcher_forwards_allow_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """`extract(url=..., allow_private=True)` reaches extract_url; the default forwards nothing."""
    seen: list[dict[str, object]] = []

    def fake_extract_url(url: str, **kwargs: object) -> object:
        seen.append(kwargs)
        sentinel = object()
        return sentinel

    monkeypatch.setattr(url_mod, "extract_url", fake_extract_url)
    extract(url="https://example.com", allow_private=True)
    extract(url="https://example.com")
    assert seen == [{"allow_private": True}, {}]


# --- (b) zip decompression-bomb guard (issue #53) -----------------------------------------------
def _zip_declaring(n_bytes: int) -> bytes:
    """A tiny archive whose single entry DECLARES ``n_bytes`` uncompressed (zeros compress well)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("blob.bin", b"\0" * n_bytes)
    return buf.getvalue()


def test_zip_bomb_blocked_for_office(monkeypatch: pytest.MonkeyPatch) -> None:
    """A high-ratio archive is rejected from DECLARED sizes — before markitdown ever runs."""
    bomb = _zip_declaring(4000)
    assert len(bomb) < 400  # genuinely high-ratio: tiny compressed, big declared
    with pytest.raises(ExtractorError, match="decompression-bomb"):
        extract_office(bomb, filename="bomb.docx", max_uncompressed_bytes=1000)


def test_zip_bomb_blocked_without_markitdown_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is stdlib-only and fires BEFORE the lazy markitdown import (extra-independent)."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "markitdown":
            raise ImportError("markitdown intentionally unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ExtractorError, match="decompression-bomb"):
        extract_office(_zip_declaring(4000), filename="bomb.docx", max_uncompressed_bytes=1000)


def test_zip_under_cap_passes_guard() -> None:
    _guard_zip_bomb(_zip_declaring(4000), filename="ok.docx", max_uncompressed_bytes=10_000)


def test_non_zip_bytes_ignored_by_guard() -> None:
    _guard_zip_bomb(b"plain text, not an archive", filename="x.docx", max_uncompressed_bytes=1)


def test_corrupt_zip_left_to_extractor_error_path() -> None:
    """A truncated zip is NOT the guard's business — markitdown's wrap still raises the error."""
    pytest.importorskip("markitdown")
    corrupt = _zip_declaring(100)[:-15]
    _guard_zip_bomb(corrupt, filename="bad.docx", max_uncompressed_bytes=10)  # guard passes it on
    with pytest.raises(ExtractorError):
        extract_office(corrupt, filename="bad.docx")


def _forged_under_declared_zip(*, real_size: int, declared: int) -> bytes:
    """A DEFLATE archive whose single entry really decompresses to ``real_size`` zeros but LIES in
    BOTH the local and central headers that it is only ``declared`` bytes.

    This is the under-declared bomb the declared-total pre-check alone cannot catch (issue #53): a
    tiny wire payload, a tiny declared size, but a large true decompression. Built by hand because
    stdlib ``zipfile`` will not write a header that disagrees with the data it compresses.
    """
    payload = b"\0" * real_size
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    comp = co.compress(payload) + co.flush()
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    fname = b"word/document.xml"
    local = (
        struct.pack(
            "<IHHHHHIIIHH", 0x04034B50, 20, 0, 8, 0, 0, crc, len(comp), declared, len(fname), 0
        )
        + fname
        + comp
    )
    central = (
        struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50,
            20,
            20,
            0,
            8,
            0,
            0,
            crc,
            len(comp),
            declared,
            len(fname),
            0,
            0,
            0,
            0,
            0,
            0,
        )
        + fname
    )
    eocd = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 1, 1, len(central), len(local), 0)
    return local + central + eocd


def test_zip_bomb_under_declared_is_caught_by_actual_decompression() -> None:
    """The DECLARED size is forgeable smaller than reality; the guard measures the ACTUAL
    decompressed size and rejects the entry that balloons past the cap (issue #53 hardening —
    the declared-total pre-check alone would wave this through)."""
    forged = _forged_under_declared_zip(real_size=8 * 1024 * 1024, declared=10)
    assert len(forged) < 20_000  # tiny wire + tiny declared, but 8 MiB of real decompression
    # Sanity: the OLD declared-only check would have passed this (declared total == 10 <= cap).
    with zipfile.ZipFile(io.BytesIO(forged)) as _zf:
        assert sum(i.file_size for i in _zf.infolist()) == 10
    with pytest.raises(ExtractorError, match="decompression-bomb"):
        _guard_zip_bomb(forged, filename="evil.docx", max_uncompressed_bytes=1024 * 1024)


def test_under_declared_bomb_blocked_via_extract_office() -> None:
    """End-to-end: extract_office rejects the under-declared bomb before markitdown decompresses it
    (the guard runs ahead of the lazy markitdown import, so this holds even without the extra)."""
    forged = _forged_under_declared_zip(real_size=8 * 1024 * 1024, declared=10)
    with pytest.raises(ExtractorError, match="decompression-bomb"):
        extract_office(forged, filename="evil.docx", max_uncompressed_bytes=1024 * 1024)


def test_default_uncompressed_cap_is_10x_per_file_cap() -> None:
    """The default = 10x the 25 MiB compressed per-file cap (a coherent expansion allowance)."""
    assert _DEFAULT_MAX_UNCOMPRESSED_BYTES == 10 * 25 * 1024 * 1024
    assert _DEFAULT_MAX_UNCOMPRESSED_BYTES <= 256 * 1024 * 1024


def test_normal_docx_passes_with_default_cap() -> None:
    """Regression: a legitimate docx extracts fine under the DEFAULT bomb cap."""
    pytest.importorskip("markitdown")
    pytest.importorskip("mammoth")
    from tests.ingest.test_extractors import _make_docx

    doc = extract_office(_make_docx("Bomb-guarded but fine"), filename="ok.docx")
    assert "Bomb-guarded but fine" in doc.markdown


# --- (b) .epub routing (ADR-0025 rec D) ---------------------------------------------------------
_EPUB_CONTAINER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="content.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)
_EPUB_OPF = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<dc:identifier id="uid">agora-test-epub</dc:identifier>'
    "<dc:title>Agora Test Book</dc:title><dc:language>en</dc:language></metadata>"
    '<manifest><item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
    '<spine><itemref idref="ch1"/></spine></package>'
)
_EPUB_XHTML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Ch 1</title></head>'
    "<body><h1>Chapter One</h1><p>Hello epub main content.</p></body></html>"
)


def _make_epub(extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _EPUB_CONTAINER)
        zf.writestr("content.opf", _EPUB_OPF)
        zf.writestr("ch1.xhtml", _EPUB_XHTML)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_extract_epub_real_via_dispatch() -> None:
    """An .epub routes through the dispatcher to markitdown and yields its chapter content."""
    pytest.importorskip("markitdown")
    doc = extract(data=_make_epub(), filename="book.epub")
    assert doc.extractor == "markitdown"
    assert doc.mime == "application/epub+zip"
    assert "Chapter One" in doc.markdown
    assert "Hello epub main content" in doc.markdown


def test_extract_epub_routes_by_mime(monkeypatch: pytest.MonkeyPatch) -> None:
    from agora_kb.ingest.extractors import office as office_mod

    sentinel = object()
    monkeypatch.setattr(office_mod, "extract_markitdown", lambda data, *, filename, **kw: sentinel)
    assert extract(data=b"x", filename="b.epub", mime="application/epub+zip") is sentinel


def test_epub_bomb_blocked() -> None:
    """A bomb entry inside an otherwise-valid epub trips the same guard."""
    bomb = _make_epub(extra={"padding.bin": b"\0" * 4000})
    with pytest.raises(ExtractorError, match="decompression-bomb"):
        extract_markitdown(bomb, filename="bomb.epub", max_uncompressed_bytes=1000)


def test_dispatcher_forwards_max_uncompressed_bytes() -> None:
    """`extract(data=..., max_uncompressed_bytes=...)` reaches the office/markitdown guard."""
    with pytest.raises(ExtractorError, match="decompression-bomb"):
        extract(data=_zip_declaring(4000), filename="bomb.docx", max_uncompressed_bytes=1000)
    with pytest.raises(ExtractorError, match="decompression-bomb"):
        extract(data=_make_epub(), filename="b.epub", max_uncompressed_bytes=10)
