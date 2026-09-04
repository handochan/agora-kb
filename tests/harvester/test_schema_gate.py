"""The ADR-0041 D6 write refusal, as it reaches the HARVEST lane (#153, ADR-0041 W2.2).

``harvest`` is not one of D6's five explicitly-named write-refusal call sites, because it writes
through :meth:`agora_kb.core.inbox.Inbox.write`, which is. That inheritance is what these tests
pin — in both directions, and against repos that really DECLARE a schema rather than the bare
``tmp_path`` the rest of :mod:`tests.harvester` uses:

* a schema-2 repo accepts gated candidates exactly as before the flip;
* a schema-1 repo refuses, writes nothing, and leaves the cursor untouched;
* the CLI renders that refusal as the one-line message every other write command prints, NOT as
  the uncaught traceback the inherited-from-``Inbox.write`` position produced;
* a directory that declares NO schema stays writable, which is the posture the rest of the
  harvester suite relies on and is a deliberate D6 rule ("declares nothing" is UNKNOWN, never
  "schema 1"), not an accident worth silently tightening.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from agora_kb.cli import main
from agora_kb.config import HarvestPolicy, ReadOnlySchemaVersionError
from agora_kb.core.hashing import content_sha256
from agora_kb.core.layout import RepoLayout
from agora_kb.harvester.connectors import ConnectorScan, HarvestedFact, Scope
from agora_kb.harvester.harvester import CursorStore, Harvester
from agora_kb.schema import Taxonomy, emit_schema

FIXED = datetime(2026, 6, 20, 9, 0, 0, tzinfo=UTC)
_POLICY = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")


class _Connector:
    """The minimal Connector Protocol implementation these tests need (mirrors test_harvester)."""

    def __init__(self) -> None:
        self.scanned = 0

    @property
    def name(self) -> str:
        return "file:demo"

    @property
    def agent(self) -> str:
        return "demo"

    @property
    def scope(self) -> Scope:
        return Scope.personal

    def scan(self, *, last_content_sha256: str | None) -> ConnectorScan:
        self.scanned += 1
        text = "- A fact worth harvesting."
        return ConnectorScan(
            unchanged=False,
            content_sha256="deadbeef",
            source_path="mem://demo",
            facts=(HarvestedFact(text=text, fact_key=content_sha256(text)),),
        )


def _declare(tmp_path: Path, schema_version: int) -> RepoLayout:
    """A repo that genuinely DECLARES ``schema_version`` in canonical ``_meta/taxonomy.yaml``.

    Through :func:`emit_schema` rather than a hand-written YAML blob, so the declaration and the
    emitted schema doc cannot disagree — the canonical reader D6 consults is
    ``_meta/taxonomy.yaml`` alone, and a fixture that wrote only that file would pass while
    describing a repo no ``agora repo init`` could produce.
    """
    layout = RepoLayout(tmp_path)
    emit_schema(layout, taxonomy=Taxonomy(schema_version=schema_version, domains=["general"]))
    return layout


def _inbox_items(layout: RepoLayout) -> list[Path]:
    return sorted(layout.inbox_dir.glob("*/*.md"))


def test_a_schema_2_repo_accepts_harvested_candidates(tmp_path: Path) -> None:
    """The flip's happy path: the lane that used to write into schema-1 repos still writes."""
    layout = _declare(tmp_path, 2)
    conn = _Connector()

    report = Harvester(layout).run([conn], policy=_POLICY, now=FIXED)

    assert report.connectors[0].status == "ok"
    assert report.connectors[0].written == 1
    assert len(_inbox_items(layout)) == 1
    assert CursorStore(layout).load("file:demo").proposed == 1


def test_a_schema_1_repo_refuses_and_writes_nothing(tmp_path: Path) -> None:
    """D6 inherited through ``Inbox.write``: a read-only repo cannot be harvested INTO.

    The cursor assertion is the load-bearing half. A refusal that still advanced the cursor would
    mark the source as scanned for a run that landed nothing, so the facts would never be offered
    again after the operator crossed to a schema-2 repo — silent loss dressed as a refusal.
    """
    layout = _declare(tmp_path, 1)

    with pytest.raises(ReadOnlySchemaVersionError):
        Harvester(layout).run([_Connector()], policy=_POLICY, now=FIXED)

    assert _inbox_items(layout) == []
    assert CursorStore(layout).load("file:demo").proposed == 0


def test_a_repo_that_declares_no_schema_is_still_writable(tmp_path: Path) -> None:
    """ "Declares nothing" is UNKNOWN, not "schema 1" — the D6 rule the rest of this suite rests on.

    An uninitialized directory has no schema-1 tree to corrupt, so refusing there would answer an
    operator in the wrong directory with a schema complaint instead of the ordinary outcome.
    """
    layout = RepoLayout(tmp_path)
    assert not (tmp_path / "_meta" / "taxonomy.yaml").exists()

    report = Harvester(layout).run([_Connector()], policy=_POLICY, now=FIXED)

    assert report.connectors[0].written == 1


@pytest.mark.parametrize("extra", [[], ["--dry-run"]])
def test_the_cli_refuses_a_schema_1_repo_before_it_scans_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str]
) -> None:
    """``agora harvest`` prints the one-line refusal, exit 1 — never a Python traceback.

    Before the gate moved into ``_cmd_harvest`` the refusal surfaced from inside
    ``Harvester.run`` and reached the terminal as an uncaught ``ReadOnlySchemaVersionError``
    stack. ``--dry-run`` is parametrized in because a preview of writes this build would refuse is
    a preview of a lie — and because the scan it would run reads another agent's memory for no
    possible benefit, which is why the check sits BEFORE ``build_connectors``.
    """
    layout = _declare(tmp_path, 1)
    sources = tmp_path / "src"
    sources.mkdir()
    (sources / "MEMORY.md").write_text("# mem\n\n- A fact.\n", encoding="utf-8")
    layout.kb_dir.mkdir(parents=True, exist_ok=True)
    (layout.kb_dir / "repo.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "demo",
                "kind": "personal",
                "harvest": {"enabled": True, "scope_lock": "personal"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "adapters.yaml").write_text(
        yaml.safe_dump(
            {
                "connectors": {
                    "file:demo": {
                        "type": "file",
                        "agent": "demo",
                        "scope": "personal",
                        "path": str(sources / "MEMORY.md"),
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    rc = main(["harvest", "--repo", str(tmp_path), *extra])
    captured = capsys.readouterr()

    assert rc == 1
    assert "READ-ONLY for this agora build" in captured.err
    assert "agora import --from-kb" in captured.err
    assert "invalid config" not in captured.err  # a schema verdict, not a malformed file
    assert "Traceback" not in captured.err
    assert _inbox_items(layout) == []
    assert CursorStore(layout).load("file:demo").proposed == 0
