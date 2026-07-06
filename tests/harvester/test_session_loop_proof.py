"""Loop-proof e2e for the session: connector — the joint step-4 exit criterion (#25, ADR-0023 §5).

Two guarantees, run through the LIVE harvest write path (Harvester → Inbox):

* **e2e-A (verbatim span-drop).** A gold pack that lands in an agent session and is echoed back into
  an assistant turn is dropped WHOLE by the ADR-0027 §8 sentinel span-strip, so it yields ZERO
  pack-derived facts — the verbatim half of the KB→session→KB loop is closed.
* **e2e-B (reworded residue).** An agent RESTATING pack content in its own words (no sentinels) is
  NOT caught by span-drop — it lands as a **gated candidate** (`kind=candidate`/`confidence=low`),
  so the curator's candidate gate (the general loop break) must re-review it every cycle. This
  INSTRUMENTS the reworded loop (ADR-0017 §5 residual); it does not claim it closed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from agora_kb.config import HarvestPolicy
from agora_kb.core.frontmatter import parse
from agora_kb.core.layout import RepoLayout
from agora_kb.harvester.connectors import Scope
from agora_kb.harvester.harvester import Harvester
from agora_kb.harvester.session_connector import SessionConnector

FIXED = datetime(2026, 7, 7, 9, 0, 0, tzinfo=UTC)
_POLICY = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="personal")

# A faithful gold pack span — the canonical ADR-0027 §8 grammar core.gold._render_span emits:
# <!-- agora:pack repo=… pack=… commit=… -->  …fact lines…  <!-- agora:pack:end … -->.
_PACK = (
    "<!-- agora:pack repo=demo pack=default commit=abc123 -->\n\n"
    "The root cause was the injected pack fact one.\n\n"
    "The fix was the injected pack fact two.\n\n"
    "<!-- agora:pack:end repo=demo pack=default commit=abc123 -->"
)


def _asst(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _write_session(tmp_path: Path, *records: dict) -> tuple[RepoLayout, SessionConnector]:
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "s.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    layout = RepoLayout(tmp_path)
    conn = SessionConnector(name="session:cc", path=str(sessions / "*.jsonl"), scope=Scope.personal)
    return layout, conn


def _inbox(layout: RepoLayout) -> list[tuple[dict, str]]:
    return [parse(p.read_text(encoding="utf-8")) for p in sorted(layout.inbox_dir.glob("*/*.md"))]


# --- e2e-A: verbatim span-drop → zero pack-derived facts ----------------------------------------


def test_e2e_a_echoed_gold_pack_yields_zero_facts(tmp_path: Path) -> None:
    # An assistant turn echoes the WHOLE pack (two marker paragraphs live INSIDE the span) plus one
    # genuine reflection OUTSIDE it — proving span-drop removes only the pack, not real knowledge.
    turn = (
        "Here is the context I was given.\n\n"
        + _PACK
        + "\n\nThe lesson learned: I must validate the diff gate myself."
    )
    layout, conn = _write_session(tmp_path, _asst(turn))

    Harvester(layout).run([conn], policy=_POLICY, now=FIXED)
    items = _inbox(layout)

    # Exactly the genuine reflection survives; the pack's two marker paragraphs contribute nothing.
    assert len(items) == 1
    body = items[0][1]
    assert "validate the diff gate" in body
    assert "injected pack fact" not in body
    assert "agora:pack" not in body  # no residual sentinel marker either


def test_e2e_a_pack_only_turn_yields_no_candidates_at_all(tmp_path: Path) -> None:
    # A turn that is ONLY the echoed pack → the whole span drops → nothing is written to the inbox.
    layout, conn = _write_session(tmp_path, _asst(_PACK))
    report = Harvester(layout).run([conn], policy=_POLICY, now=FIXED)
    assert report.total_written == 0
    assert _inbox(layout) == []


# --- e2e-B: reworded residue lands as a GATED candidate -----------------------------------------


def test_e2e_b_reworded_pack_content_lands_as_gated_candidate(tmp_path: Path) -> None:
    # The agent restates a pack claim in ITS OWN WORDS (no sentinels). Span-drop cannot catch this —
    # it MUST land, but ONLY as a gated candidate the curator re-reviews (the general loop break).
    reworded = (
        "The lesson learned: the curator is the single writer and all writes go through the inbox."
    )
    layout, conn = _write_session(tmp_path, _asst(reworded))

    Harvester(layout).run([conn], policy=_POLICY, now=FIXED)
    items = _inbox(layout)

    assert len(items) == 1
    fm, body = items[0]
    assert "curator is the single writer" in body
    # The candidate gate is the break: gated candidate, never an auto-promoted theme.
    assert fm["kind"] == "candidate"
    assert fm["confidence"] == "low"
    assert fm["source"] == "harvest:cc"


# --- validation: a personal session source is refused for a team repo (fail-closed scope) --------


def test_personal_session_refused_for_team_repo(tmp_path: Path) -> None:
    layout, conn = _write_session(tmp_path, _asst("The root cause was a leak of user prompts."))
    team_policy = HarvestPolicy(enabled=True, scope_lock="team", repo_kind="team")
    report = Harvester(layout).run([conn], policy=team_policy, now=FIXED)
    cr = report.connectors[0]
    assert cr.status == "scope-refused"  # personal transcripts can never feed a team repo
    assert _inbox(layout) == []  # nothing written past the fail-closed scope gate
