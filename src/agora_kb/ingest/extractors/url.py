"""URL extractor — fetch a web page and extract its main content to markdown (trafilatura).

trafilatura (Apache-2.0) handles both the fetch and the boilerplate-stripping main-content
extraction. We prefer markdown output where the installed version supports it, else fall back to
clean text. The optional dependency is imported lazily so the core stays dependency-light
(ADR-0005); a missing install raises :class:`ExtractorUnavailable`.
"""

from __future__ import annotations

from agora_kb.core.hashing import content_sha256

from .base import ExtractedDoc, ExtractorError, ExtractorUnavailable

__all__ = ["extract_url"]


def extract_url(url: str) -> ExtractedDoc:
    """Fetch ``url`` and extract its main content as markdown.

    Returns an :class:`ExtractedDoc` with ``extractor="url"``, ``mime="text/html"``,
    ``source_url=url``, and a best-effort ``title``.

    Raises :class:`ExtractorUnavailable` if ``trafilatura`` is not installed, and
    :class:`ExtractorError` if the page cannot be fetched or no content can be extracted (untrusted
    input — we never leak the underlying library traceback).

    .. warning::
       **SSRF (Phase-4).** This performs a server-side fetch of an arbitrary URL. In a multi-user
       deployment that is a Server-Side Request Forgery vector (internal hosts, cloud metadata
       endpoints). Phase 3 is localhost single-user, so no allowlisting/auth is applied here; URL
       allowlisting + network egress controls are deferred to Phase 4. See ADR-0004.
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

    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            raise ExtractorError(f"could not fetch URL: {url!r}")
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


def _extract_content(trafilatura: object, downloaded: str) -> str | None:
    """Extract main content, preferring markdown output when the installed version supports it.

    ``trafilatura.extract(..., output_format="markdown")`` exists on newer versions; older versions
    accept only plain ``txt``. We try markdown first and gracefully degrade to clean text.
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


def _extract_title(trafilatura: object, downloaded: str) -> str | None:
    """Best-effort document title via trafilatura metadata; ``None`` if unavailable."""
    try:
        meta = trafilatura.extract_metadata(downloaded)  # type: ignore[attr-defined]
    except Exception:
        return None
    title = getattr(meta, "title", None) if meta is not None else None
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None
