"""URL extractor — SSRF-guarded fetch + main-content extraction to markdown (trafilatura).

The **fetch** is performed here with the standard library (``http.client``) so every network
decision is under our control (issue #66): an http/https scheme allowlist, per-hop DNS resolution
with rejection of private/loopback/link-local/metadata/unique-local/unspecified addresses, and
**IP pinning** — we TCP-connect to the ALREADY-VALIDATED address while keeping the original
hostname for the ``Host`` header and the TLS SNI/certificate check, which closes the classic
DNS-rebinding race between "validate" and "connect". Redirects are followed MANUALLY and every hop
is re-resolved + re-validated (so a public page can never bounce the server into an internal
network). The **content extraction** (boilerplate-stripping main content → markdown) stays with
trafilatura (Apache-2.0), fed the fetched bytes — its ``load_html`` accepts bytestrings and does
its own encoding detection, so the extraction result matches the previous ``fetch_url`` path.

trafilatura is still the only third-party dependency and is imported lazily so the core stays
dependency-light (ADR-0005); a missing install raises :class:`ExtractorUnavailable`. The guard
itself is stdlib-only.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.parse

from agora_kb.core.hashing import content_sha256

from .base import ExtractedDoc, ExtractorError, ExtractorUnavailable

__all__ = ["extract_url"]

# --- SSRF guard knobs (issue #66) ---------------------------------------------------------------
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5
_FETCH_TIMEOUT = 30.0  # seconds — matches trafilatura's own default download timeout
# Cap the fetched body: mirrors the web face's 25 MiB per-file upload bound (a fetched page IS an
# upload by proxy), so a hostile endpoint cannot balloon server memory.
_MAX_FETCH_BYTES = 25 * 1024 * 1024
_USER_AGENT = "agora-kb/url-extractor"


def extract_url(url: str, *, allow_private: bool = False) -> ExtractedDoc:
    """Fetch ``url`` (SSRF-guarded) and extract its main content as markdown.

    Returns an :class:`ExtractedDoc` with ``extractor="url"``, ``mime="text/html"``,
    ``source_url=url``, and a best-effort ``title``.

    Raises :class:`ExtractorUnavailable` if ``trafilatura`` is not installed, and
    :class:`ExtractorError` if the URL is blocked by the SSRF guard, cannot be fetched, or yields
    no extractable content (untrusted input — we never leak the underlying library traceback).

    **SSRF guard (issue #66 — implemented; supersedes the former Phase-4 deferral note).** With the
    default ``allow_private=False`` the fetch refuses: non-http(s) schemes (``file:``, ``ftp:``,
    ``gopher:``, …); hosts whose resolved addresses include anything private (RFC1918),
    loopback (127/8, ``::1``), link-local (169.254/16 — including the 169.254.169.254 cloud
    metadata endpoint — and ``fe80::/10``), IPv6 unique-local (``fc00::/7``), unspecified
    (0.0.0.0 / ``::``), multicast, reserved, or otherwise non-global. The connection is PINNED to
    the validated address and every redirect hop is re-validated (DNS-rebinding defence). A local
    CLI caller that legitimately needs an internal URL may opt in with ``allow_private=True``; the
    web face never does (it always fetches with the guard on).
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")

    try:
        import trafilatura  # lazy: optional `ingest` extra (ADR-0005)
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise ExtractorUnavailable(
            "URL extraction requires `trafilatura`. Install the ingest extra: "
            "`pip install agora-kb[ingest]` or `uv sync --extra ingest`."
        ) from exc

    downloaded = _fetch_url_guarded(url.strip(), allow_private=allow_private)
    try:
        markdown = _extract_content(trafilatura, downloaded)
        if not markdown or not markdown.strip():
            raise ExtractorError(f"no extractable content at URL: {url!r}")
        title = _extract_title(trafilatura, downloaded)
    except ExtractorError:
        raise
    except Exception as exc:  # untrusted page → wrap, never leak a raw traceback
        raise ExtractorError(f"failed to extract URL {url!r}: {exc}") from exc

    return ExtractedDoc(
        markdown=markdown,
        title=title,
        source_url=url,
        content_sha256=content_sha256(markdown),
        mime="text/html",
        extractor="url",
    )


# --- the guarded fetch (stdlib-only; issue #66) -------------------------------------------------


def _fetch_url_guarded(url: str, *, allow_private: bool) -> bytes:
    """Fetch ``url`` with the SSRF guard on every hop; return the response body bytes.

    Each hop (the original URL and every redirect target) is: scheme-checked, DNS-resolved,
    validated (ALL resolved addresses must be public unless ``allow_private``), then fetched over a
    connection PINNED to the validated address (:func:`_open_connection`). Redirects are followed
    manually up to ``_MAX_REDIRECTS``; the body is capped at ``_MAX_FETCH_BYTES``. Any network or
    protocol failure raises :class:`ExtractorError` (untrusted input — no raw tracebacks).
    """
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        scheme, host, port, path = _parse_target(current)
        ips = _resolve_host(host, port)
        if not allow_private:
            _reject_non_public(host, ips, current)
        conn = _open_connection(scheme, host, ips[0], port, timeout=_FETCH_TIMEOUT)
        try:
            conn.request(
                "GET",
                path,
                # identity: http.client does not auto-decompress, and trafilatura expects the
                # raw (uncompressed) HTML bytes.
                headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"},
            )
            resp = conn.getresponse()
            if resp.status in _REDIRECT_STATUSES:
                location = resp.getheader("Location")
                if not location:
                    raise ExtractorError(
                        f"could not fetch URL {current!r}: redirect (HTTP {resp.status}) "
                        "without a Location header"
                    )
                # Re-enter the loop: the redirect TARGET is re-parsed, re-resolved, and
                # re-validated exactly like a first-class URL (per-hop SSRF defence).
                current = urllib.parse.urljoin(current, location)
                continue
            if resp.status != 200:
                raise ExtractorError(f"could not fetch URL {current!r}: HTTP {resp.status}")
            body = resp.read(_MAX_FETCH_BYTES + 1)
            if len(body) > _MAX_FETCH_BYTES:
                raise ExtractorError(
                    f"page too large at {current!r}: more than {_MAX_FETCH_BYTES} bytes"
                )
            return bytes(body)
        except ExtractorError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            # ssl.SSLError / socket.timeout are OSError subclasses — one tidy wrap for them all.
            raise ExtractorError(f"could not fetch URL {current!r}: {exc}") from exc
        finally:
            conn.close()
    raise ExtractorError(f"could not fetch URL {url!r}: too many redirects (> {_MAX_REDIRECTS})")


def _parse_target(url: str) -> tuple[str, str, int, str]:
    """Split ``url`` into ``(scheme, host, port, path_with_query)``; reject non-http(s) schemes."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port  # property access may raise ValueError on a malformed port
    except ValueError as exc:
        raise ExtractorError(f"invalid URL {url!r}: {exc}") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ExtractorError(
            f"blocked URL {url!r}: scheme {parts.scheme or '(none)'!r} is not allowed "
            "(only http/https; SSRF guard)"
        )
    host = parts.hostname
    if not host:
        raise ExtractorError(f"invalid URL (no host): {url!r}")
    if port is None:
        port = 443 if scheme == "https" else 80
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return scheme, host, port, path


def _resolve_host(host: str, port: int) -> list[str]:
    """Resolve ``host`` to ALL of its addresses (the DNS seam tests monkeypatch).

    An IP literal passes straight through ``getaddrinfo`` without a DNS query. Resolution failure
    is an :class:`ExtractorError` (matching the old "could not fetch" tolerance).
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ExtractorError(f"could not resolve URL host {host!r}: {exc}") from exc
    ips: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in infos:
        ip = str(sockaddr[0])
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise ExtractorError(f"could not resolve URL host {host!r}: no addresses returned")
    return ips


def _reject_non_public(host: str, ips: list[str], url: str) -> None:
    """Raise :class:`ExtractorError` if ANY resolved address is non-public (fail closed, #66)."""
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip.split("%", 1)[0])  # strip an IPv6 zone id if present
        except ValueError as exc:
            raise ExtractorError(
                f"blocked URL {url!r}: unparseable address {ip!r} for host {host!r}"
            ) from exc
        label = _classify_blocked(addr)
        if label is not None:
            raise ExtractorError(
                f"blocked URL {url!r}: host {host!r} resolves to {label} address {ip} "
                "(SSRF guard — private/internal networks are not fetchable from the server; "
                "a local CLI caller may opt in with allow_private)"
            )


def _classify_blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return the blocked-category label for ``addr``, or ``None`` when it is a public address."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped  # ::ffff:10.0.0.1 must be judged as 10.0.0.1
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:
        # Includes the 169.254.169.254 cloud-metadata endpoint and fe80::/10.
        return "link-local"
    if addr.is_private:
        return "private"  # RFC1918 + IPv6 unique-local fc00::/7 (+ other special-use ranges)
    if addr.is_unspecified:
        return "unspecified"
    if addr.is_multicast:
        return "multicast"
    if addr.is_reserved:
        return "reserved"
    if not addr.is_global:
        return "non-global special-purpose"  # backstop: CGNAT 100.64/10, benchmarking nets, …
    return None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Plain-HTTP connection that TCP-connects to a pre-validated IP, keeping ``Host: <host>``."""

    def __init__(self, host: str, ip: str, port: int, *, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self) -> None:  # pragma: no cover - real sockets; the seam is _open_connection
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a pre-validated IP; TLS still verifies the ORIGINAL hostname."""

    def __init__(self, host: str, ip: str, port: int, *, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self) -> None:  # pragma: no cover - real sockets; the seam is _open_connection
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        # SNI + the certificate hostname check run against the ORIGINAL host, not the pinned IP —
        # the server sees a normal TLS handshake while we control exactly where the TCP goes.
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _open_connection(
    scheme: str, host: str, ip: str, port: int, *, timeout: float
) -> http.client.HTTPConnection:
    """Open a connection PINNED to the already-validated ``ip`` (the network seam tests mock)."""
    if scheme == "https":
        return _PinnedHTTPSConnection(host, ip, port, timeout=timeout)
    return _PinnedHTTPConnection(host, ip, port, timeout=timeout)


# --- trafilatura extraction (unchanged mechanics) -----------------------------------------------


def _extract_content(trafilatura: object, downloaded: str | bytes) -> str | None:
    """Extract main content, preferring markdown output when the installed version supports it.

    ``trafilatura.extract(..., output_format="markdown")`` exists on newer versions; older versions
    accept only plain ``txt``. We try markdown first and gracefully degrade to clean text.
    ``downloaded`` may be raw bytes — trafilatura's ``load_html`` accepts bytestrings and performs
    its own encoding detection (same behaviour its ``fetch_url`` relied on).
    """
    extract_fn = trafilatura.extract  # type: ignore[attr-defined]
    try:
        result = extract_fn(downloaded, output_format="markdown")
        if result:
            return result
    except (TypeError, ValueError):
        # Older trafilatura: no `output_format="markdown"` support — fall through to clean text.
        pass
    return extract_fn(downloaded)


def _extract_title(trafilatura: object, downloaded: str | bytes) -> str | None:
    """Best-effort document title via trafilatura metadata; ``None`` if unavailable."""
    try:
        meta = trafilatura.extract_metadata(downloaded)  # type: ignore[attr-defined]
    except Exception:
        return None
    title = getattr(meta, "title", None) if meta is not None else None
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None
