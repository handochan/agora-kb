"""Lock the team-deployment guide (docs/DEPLOY-TEAM.md, issue #68) to the landed code.

The guide is operator-facing prose, not code — these tests keep its load-bearing claims from
silently rotting against the implementation:

* the comprehensive ``_kb/repo.yaml`` example must parse **through the real config loaders**
  (``load_repo_config`` / ``load_backup_policy`` / ``load_web_config`` / ``load_harvest_policy``)
  and every documented key must land on the field it names — the example deliberately uses
  NON-default values so a renamed/dropped key fails loudly instead of "parsing" into a default;
* the ``kind: team`` scope-gate nuance the guide states (undeclared kind → treated team →
  personal sources fail-closed; declaring team is what admits team-scope connectors) must match
  :func:`agora_kb.harvester.harvester.check_scope`;
* the Caddy/nginx examples must keep the #67 trust-boundary elements (basic auth, force-SET
  identity header, loopback upstream) plus the guide's own §2 additions (``/metrics`` ·
  ``/dashboard`` · ``/api/dashboard`` blocked);
* no fenced example may ever bind a non-loopback address (``0.0.0.0``) — the prohibition itself
  must stay stated in prose;
* the SSH forced-command recipe must keep ``command="…"`` + ``restrict`` + per-key ``--writer``;
* the cross-links (README ↔ deploy/README ↔ this guide) must exist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from agora_kb.config import (
    HarvestPolicy,
    load_backup_policy,
    load_harvest_policy,
    load_repo_config,
    load_web_config,
    repo_config_path,
)
from agora_kb.core.layout import RepoLayout
from agora_kb.curator.constants import DEFAULT_MAX_CANDIDATES_PER_RUN
from agora_kb.harvester.connectors import Scope
from agora_kb.harvester.harvester import ScopeViolation, check_scope

REPO_ROOT = Path(__file__).resolve().parents[1]
GUIDE_PATH = REPO_ROOT / "docs" / "DEPLOY-TEAM.md"
DEPLOY_README_PATH = REPO_ROOT / "deploy" / "README.md"
README_PATH = REPO_ROOT / "README.md"

# ```info\n body ``` — well-formed fenced code blocks (the guide is hand-written markdown).
_FENCE_RE = re.compile(r"^```([^\n`]*)\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)


def _guide_text() -> str:
    return GUIDE_PATH.read_text(encoding="utf-8")


def _fenced_blocks() -> list[tuple[str, str]]:
    """All fenced code blocks in the guide as ``(info_string, body)`` pairs."""
    return [(m.group(1).strip(), m.group(2)) for m in _FENCE_RE.finditer(_guide_text())]


def _block_by_info(info: str) -> str:
    bodies = [body for i, body in _fenced_blocks() if i == info]
    assert bodies, f"guide must contain a ```{info} fenced block"
    assert len(bodies) == 1, f"expected exactly one ```{info} block, found {len(bodies)}"
    return bodies[0]


# --- existence + structure -----------------------------------------------------------------------


def test_guide_exists_with_all_five_sections_and_checklist() -> None:
    assert GUIDE_PATH.is_file(), "docs/DEPLOY-TEAM.md (issue #68) missing"
    text = _guide_text()
    for heading in (
        "## 1. 토폴로지",
        "## 2. Reverse proxy",
        "## 3. MCP 쓰기",
        "## 4. 읽기 배포",
        "## 5. 비밀 취급",
        "## 설치 체크리스트",
    ):
        assert heading in text, f"guide must keep the {heading!r} section"


# --- §1: the comprehensive repo.yaml example parses through the REAL loaders ---------------------


def _example_repo_yaml() -> str:
    """The §1 comprehensive ``_kb/repo.yaml`` example (the yaml block naming that file)."""
    body = _block_by_info("yaml")
    assert "_kb/repo.yaml" in body.splitlines()[0], (
        "the guide's yaml block must be the _kb/repo.yaml comprehensive example"
    )
    return body


def test_repo_yaml_example_is_valid_yaml() -> None:
    data = yaml.safe_load(_example_repo_yaml())
    assert isinstance(data, dict)


def test_repo_yaml_example_keys_land_via_real_loaders(tmp_path: Path) -> None:
    """Every key the guide documents must be CONSUMED by the loader it names.

    The example uses non-default values on purpose: if a key were renamed in config.py (or the
    guide typo'd one), the assertion would see the documented default instead and fail — this is
    the guide→code drift lock the #68 task asks for.
    """
    layout = RepoLayout(tmp_path)
    layout.kb_dir.mkdir(parents=True, exist_ok=True)
    repo_config_path(layout).write_text(_example_repo_yaml(), encoding="utf-8")

    cfg = load_repo_config(layout)
    assert cfg.kind == "team"  # default: "personal"
    assert cfg.name == "engineering"
    # curator.limits.max_candidates_per_run (#60) — 64 in the example, default 32.
    assert cfg.max_candidates_per_run == 64
    assert cfg.max_candidates_per_run != DEFAULT_MAX_CANDIDATES_PER_RUN

    backup = load_backup_policy(layout)
    assert backup.remote == "git@git.example.com:team/kb-mirror.git"  # default: None (off)
    assert backup.auto is True  # default: False

    web = load_web_config(layout)
    assert web.identity.trusted_header == "X-Remote-User"  # default: None (feature off)
    assert web.identity.strip_domain is True  # default: False
    assert web.upload.url_enabled is False  # default: True (#66 off-switch)

    harvest = load_harvest_policy(layout)
    # The top-level `kind: team` must reach the scope gate's repo_kind input (§5).
    assert harvest.repo_kind == "team"
    # The harvest block is deliberately commented out in the example — opt-in stays off.
    assert harvest.enabled is False


# --- §5: the kind/scope-gate nuance must match check_scope ---------------------------------------


def test_undeclared_kind_is_treated_team_so_personal_sources_fail_closed() -> None:
    # The guide: "미선언이면 team으로 취급 … personal 소스는 선언 없이도 이미 거부된다".
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind=None)
    with pytest.raises(ScopeViolation):
        check_scope(Scope.personal, policy)


def test_declared_team_kind_admits_team_scope_connectors() -> None:
    # The guide: declaring kind: team is what admits team-scope connectors (+ scope_lock: team).
    check_scope(Scope.team, HarvestPolicy(enabled=True, scope_lock="team", repo_kind="team"))


def test_personal_source_never_feeds_a_team_repo() -> None:
    policy = HarvestPolicy(enabled=True, scope_lock="personal", repo_kind="team")
    with pytest.raises(ScopeViolation):
        check_scope(Scope.personal, policy)


# --- §2: proxy examples keep the trust-boundary elements -----------------------------------------


def test_caddy_example_keeps_required_elements() -> None:
    body = _block_by_info("caddyfile")
    assert "basic_auth" in body, "Caddy example must authenticate (basic_auth)"
    assert "header_up X-Remote-User" in body, "Caddy must force-SET the identity header (#67)"
    assert "reverse_proxy 127.0.0.1:8000" in body, "upstream must stay the loopback bind"
    # Match the full handle directive: a bare "/dashboard" substring check would be trivially
    # satisfied by "/api/dashboard" (the JSON twin routes, app.py /api/dashboard/*).
    for path in ("/metrics", "/dashboard", "/api/dashboard"):
        assert f"handle {path}*" in body, f"Caddy example must block the internal-only {path} path"
    assert "403" in body, "the blocked internal paths must answer 403"


def test_nginx_example_keeps_required_elements() -> None:
    body = _block_by_info("nginx")
    assert "auth_basic" in body and "auth_basic_user_file" in body, "nginx must authenticate"
    assert "proxy_set_header X-Remote-User" in body, "nginx must force-SET the identity header"
    assert "proxy_pass http://127.0.0.1:8000" in body, "upstream must stay the loopback bind"
    assert "listen 443 ssl" in body, "nginx example must terminate TLS"
    for path in ("location /metrics", "location /dashboard", "location /api/dashboard"):
        assert path in body, f"nginx example must block the internal-only path ({path})"
    assert "return 403" in body


def test_no_fenced_example_binds_a_public_address() -> None:
    """No code/config example may ever show a non-loopback bind — prose states the ban."""
    for info, body in _fenced_blocks():
        assert "0.0.0.0" not in body, (
            f"a fenced ```{info} block contains a 0.0.0.0 bind example — the guide must never "
            f"demonstrate a public bind (the web face has no auth/TLS)"
        )
    # …and the prohibition itself must remain STATED (prose only).
    assert "0.0.0.0" in _guide_text(), "the guide must keep the explicit 0.0.0.0-bind prohibition"


# --- §3: SSH forced-command recipe ---------------------------------------------------------------


def test_authorized_keys_example_pins_command_restrict_and_per_key_writer() -> None:
    blocks = [body for _, body in _fenced_blocks() if 'command="' in body]
    assert len(blocks) == 1, "guide must contain exactly one authorized_keys forced-command block"
    body = blocks[0]
    entries = [line for line in body.splitlines() if line.strip()]
    assert len(entries) >= 2, "example must show per-key pinning (at least two keys)"
    writers = set()
    for entry in entries:
        assert entry.startswith('command="'), "every entry must be a forced command"
        assert ",restrict " in entry, "every entry must carry the restrict option"
        assert "agora serve" in entry, "the forced command must be the MCP stdio face"
        match = re.search(r"--writer (\S+)", entry)
        assert match, "every forced command must pin --writer"
        writers.add(match.group(1))
    assert len(writers) == len(entries), "each key must pin a DISTINCT writer identity"


def test_guide_states_kb_curate_exposure_discipline() -> None:
    text = _guide_text()
    assert "kb_curate" in text and "force" in text, "guide must state the kb_curate/force exposure"
    assert "rate-limit" in text, "guide must state that no rate-limit exists"


# --- §4/§5: landed channels + gates the guide leans on -------------------------------------------


def test_guide_references_only_landed_channels_and_open_gates() -> None:
    text = _guide_text()
    # Remote gold consumption (#40) — the clone can never carry _kb/gold/.
    assert "kb_context" in text and "/api/gold/" in text
    # MCP navigation loop (#58).
    assert "kb_read" in text and "kb_neighbors" in text
    # Open gates the guide must keep naming (not silently drop): multi-machine ADR + retention ADR.
    assert "#46" in text, "curation-home rule must reference the #46 multi-machine ADR"
    assert "ADR-0031" in text, "mail:/chat: prohibition must reference ADR-0031 (#42)"
    assert "harvest.redact" in text, "§5 must name the only redaction boundary (harvest.redact)"


# --- cross-links ---------------------------------------------------------------------------------


def test_cross_links_exist_in_both_directions() -> None:
    assert "deploy/README.md" in _guide_text(), "guide must reference deploy/README.md (#65/#67)"
    assert "DEPLOY-TEAM.md" in DEPLOY_README_PATH.read_text(encoding="utf-8"), (
        "deploy/README.md must cross-link the team guide"
    )
    assert "docs/DEPLOY-TEAM.md" in README_PATH.read_text(encoding="utf-8"), (
        "the top-level README web/deploy sections must point at the team guide"
    )
