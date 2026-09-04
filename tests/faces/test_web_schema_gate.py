"""The ADR-0041 D6 write refusal, as it reaches the WEB UPLOAD lane (#153, ADR-0041 W2.2).

The web face is the fourth surface that captures into the inbox (after ``kb_remember``,
``agora harvest`` and the CLI write commands) and, like the harvester, it is not on D6's list of
explicitly-named call sites: it inherits the gate from ``Inbox.write`` at the end of the ADR-0020
upload write path. What matters is therefore how the inherited refusal PRESENTS — a schema verdict
the caller can act on, not an unhandled 500 — and that a schema-2 repo is unaffected.

All THREE upload lanes are covered, because they diverge in how a failure reaches the caller:
``POST /api/upload`` answers with a 4xx + ``detail``, ``POST /api/upload-batch`` answers 200 with a
per-file ``FileReceipt``, and the HTMX form re-renders a receipt fragment. They share one write
seam, and these tests are what keep the refusal arm on that seam rather than in three places.

No ``src/agora_kb/faces/web`` behaviour is asserted beyond that; the read routes' schema-2 story is
wave W2.3.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agora_kb.core import Repo  # noqa: E402
from agora_kb.faces.web import build_app  # noqa: E402
from agora_kb.schema import Taxonomy, emit_schema  # noqa: E402

_UPLOAD = {"file": ("note.md", b"# N\n\nUnbilled receivables are recognized at month end.\n")}


def _client(tmp_path: Path, *, schema_version: int) -> TestClient:
    """A web face over a repo that genuinely DECLARES ``schema_version`` in ``_meta/taxonomy.yaml``.

    ``force=True`` because ``Repo.init`` has already seeded the tree: the canonical declaration is
    what D6's reader consults, so it has to be the emitted one rather than a second opinion.
    """
    repo = Repo.resolve(tmp_path)
    repo.init()
    emit_schema(
        repo.layout,
        taxonomy=Taxonomy(schema_version=schema_version, domains=["general"]),
        force=True,
    )
    return TestClient(
        build_app(repo_path=tmp_path), base_url="http://localhost", raise_server_exceptions=False
    )


def test_upload_into_a_schema_2_repo_is_captured(tmp_path: Path) -> None:
    """The flip's happy path for the web capture face."""
    response = _client(tmp_path, schema_version=2).post(
        "/api/upload", files=_UPLOAD, headers={"Origin": "http://localhost"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queued"] is True
    assert body["inbox_depth"] == 1


def test_upload_into_a_schema_1_repo_is_refused_as_a_schema_verdict(tmp_path: Path) -> None:
    """A read-only repo refuses the capture with an actionable 4xx, never an unhandled 500.

    ``raise_server_exceptions=False`` on the client is what makes the distinction real: an
    ``Inbox.write`` refusal that escaped the route's own handling would surface here as a 500 with
    no message the operator could act on, which is exactly the shape ``agora harvest`` had before
    its gate moved out of ``Harvester.run``.
    """
    client = _client(tmp_path, schema_version=1)

    response = client.post("/api/upload", files=_UPLOAD, headers={"Origin": "http://localhost"})

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "READ-ONLY for this agora build" in detail
    assert "agora import --from-kb" in detail
    # The verdict is rendered UNPREFIXED. The generic ValueError arm behind this route exists for
    # malformed input and says "could not capture upload: …", which frames a repo-level schema
    # verdict as a bad file; the operator's file is fine and their repo is the old half.
    assert not detail.startswith("could not capture upload")
    # And the read side is untouched by the write refusal — D6's whole point.
    assert client.get("/api/status").status_code == 200


def test_batch_upload_into_a_schema_1_repo_is_a_PER_FILE_receipt_error(
    tmp_path: Path,
) -> None:
    """The multi-upload lane renders the refusal as each file's own receipt, never as a 500.

    ``POST /api/upload-batch`` is best-effort per file (ADR-0025): the batch itself succeeds with
    200 and every file carries its own outcome. A schema refusal that escaped the shared write seam
    would take the whole request down with it and discard the outcomes of files that had already
    been read — so the property to pin is that the D6 verdict arrives THROUGH the receipt shape,
    once per file, with the crossing named.
    """
    client = _client(tmp_path, schema_version=1)

    response = client.post(
        "/api/upload-batch",
        files=[
            ("files", ("a.md", b"# A\n\nUnbilled receivables are recognized at month end.\n")),
            ("files", ("b.md", b"# B\n\nDeferred revenue unwinds over the contract term.\n")),
        ],
        headers={"Origin": "http://localhost"},
    )

    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert [r["filename"] for r in results] == ["a.md", "b.md"]
    for receipt in results:
        assert receipt["id"] is None
        assert receipt["queued"] is False
        assert "READ-ONLY for this agora build" in receipt["error"]
        assert "agora import --from-kb" in receipt["error"]


def test_the_html_upload_form_renders_the_refusal_instead_of_a_server_error(
    tmp_path: Path,
) -> None:
    """The HTMX form runs the SAME pipeline, so the human lane must get the same actionable text.

    An unhandled refusal here would swap the receipt fragment for the browser's own 500 page —
    the one failure shape in which the operator learns nothing about what to do next.
    """
    client = _client(tmp_path, schema_version=1)

    response = client.post(
        "/upload",
        files={"file": ("note.md", b"# N\n\nUnbilled receivables are recognized at month end.\n")},
        headers={"Origin": "http://localhost", "HX-Request": "true"},
    )

    assert response.status_code == 422, response.text
    assert "READ-ONLY for this agora build" in response.text
    assert "agora import --from-kb" in response.text


def test_the_dashboard_health_and_metrics_say_the_repo_is_read_only(tmp_path: Path) -> None:
    """The READ surfaces name the write verdict too — otherwise the dashboard shows a green KB.

    A schema-1 repo reads and lints perfectly, so ``lint_ok: true`` / zero findings is a TRUE
    statement that leaves a false impression: every upload into that repo comes back as the receipt
    error the tests above pin. The health payload, the rendered panel and ``/metrics`` all key on
    the same canonical declaration ``Inbox.write`` consults, so no two of them can disagree.
    """
    client = _client(tmp_path, schema_version=1)

    health = client.get("/api/dashboard/health").json()
    assert health["kb_schema_version"] == 1
    assert health["writable_schema"] is False
    assert health["lint_ok"] is True  # ...the true-but-misleading half, still reported honestly

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "READ-ONLY" in page.text
    assert "agora import --from-kb" in page.text

    metrics = client.get("/metrics")
    if metrics.status_code == 200:  # the `metrics` extra is optional
        assert "agora_kb_schema_version 1.0" in metrics.text
        assert "agora_kb_schema_writable 0.0" in metrics.text


def test_a_schema_2_repo_reports_itself_writable_on_the_same_surfaces(tmp_path: Path) -> None:
    """The verdict is a verdict, not a banner: the writable repo says so and shows no remedy."""
    client = _client(tmp_path, schema_version=2)

    health = client.get("/api/dashboard/health").json()
    assert health["kb_schema_version"] == 2
    assert health["writable_schema"] is True

    page = client.get("/dashboard")
    assert "READ-ONLY" not in page.text

    metrics = client.get("/metrics")
    if metrics.status_code == 200:
        assert "agora_kb_schema_writable 1.0" in metrics.text
