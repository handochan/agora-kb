"""The ADR-0041 D6 write refusal, as it reaches the WEB UPLOAD lane (#153, ADR-0041 W2.2).

The web face is the fourth surface that captures into the inbox (after ``kb_remember``,
``agora harvest`` and the CLI write commands) and, like the harvester, it is not on D6's list of
explicitly-named call sites: it inherits the gate from ``Inbox.write`` at the end of the ADR-0020
upload write path. What matters is therefore how the inherited refusal PRESENTS — a schema verdict
the caller can act on, not an unhandled 500 — and that a schema-2 repo is unaffected.

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
    # And the read side is untouched by the write refusal — D6's whole point.
    assert client.get("/api/status").status_code == 200
