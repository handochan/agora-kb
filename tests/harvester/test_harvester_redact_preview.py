"""Tests for the ``--dry-run`` would-redact preview (issue #39, ADR-0023 decision 5).

The preview is REDACTED (a secret must NEVER reach the terminal), the notes are metadata-only
(class + count), the dry-run is side-effect-free, and the shipped WRITE path stays byte-identical —
live write-path redaction lands with the session connector (#25), so today the inbox still receives
the RAW fact text verbatim.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from agora_kb.cli import main
from agora_kb.config import HarvestPolicy
from agora_kb.core.frontmatter import parse
from agora_kb.core.hashing import content_sha256
from agora_kb.core.layout import RepoLayout
from agora_kb.harvester.connectors import ConnectorScan, HarvestedFact, Scope
from agora_kb.harvester.harvester import CursorStore, Harvester

FIXED = datetime(2026, 7, 5, 9, 0, 0, tzinfo=UTC)
_SECRET = "AKIAIOSFODNN7EXAMPLE"  # an AWS access-key-id shape (default-on class)
_POLICY = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")


class FakeConnector:
    """Minimal in-memory connector (mirrors the one in test_harvester.py)."""

    def __init__(self, name: str, agent: str, scope: Scope, facts: list[HarvestedFact]) -> None:
        self._name, self._agent, self._scope, self._facts = name, agent, scope, facts

    @property
    def name(self) -> str:
        return self._name

    @property
    def agent(self) -> str:
        return self._agent

    @property
    def scope(self) -> Scope:
        return self._scope

    def scan(self, *, last_content_sha256: str | None) -> ConnectorScan:
        return ConnectorScan(
            unchanged=False,
            content_sha256="deadbeef",
            source_path=f"mem://{self._agent}",
            facts=tuple(self._facts),
        )


def _fact(text: str) -> HarvestedFact:
    return HarvestedFact(text=text, fact_key=content_sha256(text))


def _read_inbox(layout: RepoLayout) -> list[tuple[dict, str]]:
    return [parse(p.read_text(encoding="utf-8")) for p in sorted(layout.inbox_dir.glob("*/*.md"))]


def _inbox_files(layout: RepoLayout) -> list[Path]:
    return list(layout.inbox_dir.glob("*/*.md")) if layout.inbox_dir.exists() else []


# --- dry-run preview: redacted text + metadata-only notes ---------------------------------------


def test_dry_run_preview_is_redacted_not_the_secret(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    conn = FakeConnector(
        "file:demo", "demo", Scope.personal, [_fact(f"- note carrying {_SECRET} inline")]
    )
    cr = Harvester(layout).run([conn], policy=_POLICY, now=FIXED, dry_run=True).connectors[0]

    assert cr.dry_run and cr.status == "ok" and cr.facts_found == 1
    # The preview text is what the CLI renders — it must be redacted, never the raw secret.
    assert all(_SECRET not in f.text for f in cr.preview)
    assert any("[REDACTED:aws_access_key_id]" in f.text for f in cr.preview)
    # A metadata-only note reports class + count, and never the secret itself.
    assert any("would redact 1 fact" in n and "aws_access_key_id x1" in n for n in cr.notes)
    assert all(_SECRET not in n for n in cr.notes)


def test_dry_run_note_aggregates_multiple_classes_in_one_fact(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    fact = _fact(f"- {_SECRET} and token ghp_" + "a" * 36)
    conn = FakeConnector("file:demo", "demo", Scope.personal, [fact])
    cr = Harvester(layout).run([conn], policy=_POLICY, now=FIXED, dry_run=True).connectors[0]

    note = next(n for n in cr.notes if "would redact" in n)
    assert "would redact 1 fact" in note
    # both classes appear, class-sorted (aws before github).
    assert "aws_access_key_id x1, github_token x1" in note
    assert all(_SECRET not in f.text and "ghp_" not in f.text for f in cr.preview)


def test_dry_run_note_accumulates_counts_across_facts(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    facts = [_fact(f"- first {_SECRET}"), _fact(f"- second {_SECRET}")]
    conn = FakeConnector("file:demo", "demo", Scope.personal, facts)
    cr = Harvester(layout).run([conn], policy=_POLICY, now=FIXED, dry_run=True).connectors[0]

    note = next(n for n in cr.notes if "would redact" in n)
    assert "would redact 2 fact(s) before persistence: aws_access_key_id x2" in note


def test_dry_run_clean_facts_add_no_redaction_note(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    conn = FakeConnector("file:demo", "demo", Scope.personal, [_fact("- an ordinary bullet")])
    cr = Harvester(layout).run([conn], policy=_POLICY, now=FIXED, dry_run=True).connectors[0]

    assert not any("would redact" in n for n in cr.notes)
    assert cr.preview[0].text == "- an ordinary bullet"  # clean text is unchanged


def test_dry_run_touches_neither_inbox_nor_cursor(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    conn = FakeConnector("file:demo", "demo", Scope.personal, [_fact(f"- {_SECRET}")])
    Harvester(layout).run([conn], policy=_POLICY, now=FIXED, dry_run=True)

    assert _inbox_files(layout) == []
    cur = CursorStore(layout).load("file:demo")
    assert cur.proposed == 0 and cur.last_scan is None


# --- write-path regression lock: raw text persists verbatim (redaction is deferred to #25) ------


def test_write_path_is_byte_identical_persists_raw_text(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    text = f"- credential {_SECRET} noted"
    conn = FakeConnector("file:demo", "demo", Scope.personal, [_fact(text)])
    report = Harvester(layout).run([conn], policy=_POLICY, now=FIXED, dry_run=False)

    assert report.connectors[0].written == 1
    items = _read_inbox(layout)
    assert len(items) == 1
    _fm, body = items[0]
    # #39 does NOT redact on the write path — the inbox item carries the RAW text verbatim.
    assert _SECRET in body
    assert "[REDACTED" not in body


# --- CLI stdout e2e: the security blocker — the secret must never reach the terminal ------------


def test_cli_dry_run_stdout_never_prints_the_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = RepoLayout(tmp_path)
    layout.kb_dir.mkdir(parents=True, exist_ok=True)
    mem = tmp_path / "MEMORY.md"
    mem.write_text(f"# mem\n\n- a fact carrying {_SECRET} inline\n", encoding="utf-8")
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
        yaml.safe_dump({"connectors": {"file:demo": {"path": str(mem), "scope": "personal"}}}),
        encoding="utf-8",
    )

    rc = main(["harvest", "--repo", str(tmp_path), "--dry-run"])
    out = capsys.readouterr().out

    assert rc == 0
    assert _SECRET not in out  # THE blocker: the raw secret must not be printed to stdout
    assert "[REDACTED:aws_access_key_id]" in out
    assert "would redact" in out
    assert _inbox_files(layout) == []  # dry-run wrote nothing
