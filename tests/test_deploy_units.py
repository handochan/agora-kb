"""Lock the deploy/ always-on unit files (issue #65).

The launchd plists and systemd units under ``deploy/`` are operator-facing templates, not code —
these tests keep them from silently rotting: they must exist, parse with stdlib parsers
(``plistlib`` / ``configparser``), carry the keys a service manager actually needs
(``Label``/``ProgramArguments``/``StandardOutPath``, ``[Service] ExecStart``, timer schedule),
keep the web face pinned to ``127.0.0.1`` (never ``0.0.0.0`` — no auth/SSRF-guard/TLS), use the
``uv run --directory`` invocation form (the only supported run form while the package is
unreleased), and retain the FIXED placeholder tokens so a future edit that drops one is caught.
On macOS, ``plutil -lint`` additionally syntax-checks the plists (skipped where absent).
"""

from __future__ import annotations

import configparser
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
LAUNCHD_DIR = DEPLOY_DIR / "launchd"
SYSTEMD_DIR = DEPLOY_DIR / "systemd"

PLIST_NAMES = ["com.agora.watch.plist", "com.agora.web.plist", "com.agora.harvest.plist"]
SERVICE_NAMES = ["agora-watch.service", "agora-web.service", "agora-harvest.service"]
TIMER_NAME = "agora-harvest.timer"

ALL_UNIT_PATHS = (
    [LAUNCHD_DIR / n for n in PLIST_NAMES]
    + [SYSTEMD_DIR / n for n in SERVICE_NAMES]
    + [SYSTEMD_DIR / TIMER_NAME]
)

# The FIXED substitution tokens (see deploy/README.md "Placeholders"). Every unit file must
# carry the path token so `grep ABSOLUTE/PATH/TO` catches a missed substitution uniformly.
PATH_PLACEHOLDER = "/ABSOLUTE/PATH/TO/"
USER_PLACEHOLDER = "YOUR_USER"


def _load_plist(name: str) -> dict:
    with (LAUNCHD_DIR / name).open("rb") as fh:
        return plistlib.load(fh)


def _load_unit(path: Path) -> configparser.ConfigParser:
    """Parse a systemd unit file with configparser (INI-shaped; no interpolation, keep case)."""
    parser = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    parser.optionxform = str  # type: ignore[method-assign]  # preserve ExecStart/OnBootSec case
    parser.read_string(path.read_text(encoding="utf-8"))
    return parser


# --- existence -----------------------------------------------------------------------------------


def test_all_unit_files_exist() -> None:
    missing = [str(p) for p in ALL_UNIT_PATHS if not p.is_file()]
    assert not missing, f"deploy unit file(s) missing: {missing}"
    assert (DEPLOY_DIR / "README.md").is_file(), "deploy/README.md (install guide) missing"


# --- launchd plists ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", PLIST_NAMES)
def test_plist_parses_with_required_keys(name: str) -> None:
    data = _load_plist(name)
    # Label must match the filename stem — launchctl addresses the job by Label.
    assert data["Label"] == name.removesuffix(".plist")
    argv = data["ProgramArguments"]
    assert isinstance(argv, list) and argv, "ProgramArguments must be a non-empty array"
    assert all(isinstance(a, str) for a in argv)
    # Unattended runs must capture output — silent-death debugging depends on these.
    assert data["StandardOutPath"], "StandardOutPath required (log capture)"
    assert data["StandardErrorPath"], "StandardErrorPath required (log capture)"


@pytest.mark.parametrize("name", PLIST_NAMES)
def test_plist_uses_uv_run_directory_form(name: str) -> None:
    argv = _load_plist(name)["ProgramArguments"]
    # The only supported run form today: `uv run --directory <checkout> agora <cmd> ...`
    # (package version 0.0.0, unreleased — there is no installed `agora` binary to point at).
    assert argv[0].endswith("/uv"), f"argv[0] must be an absolute uv path, got {argv[0]!r}"
    assert argv[1] == "run"
    assert "--directory" in argv
    assert "agora" in argv
    assert "--repo" in argv


def test_watch_plist_is_a_keepalive_daemon() -> None:
    data = _load_plist("com.agora.watch.plist")
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True
    assert "watch" in data["ProgramArguments"]


def test_web_plist_binds_loopback_only() -> None:
    argv = _load_plist("com.agora.web.plist")["ProgramArguments"]
    assert "web" in argv
    host_idx = argv.index("--host")
    assert argv[host_idx + 1] == "127.0.0.1", "web unit must hard-code the loopback host"
    assert not any("0.0.0.0" in a for a in argv), (
        "the web face has no auth/SSRF-guard/TLS — it must NEVER bind 0.0.0.0"
    )


def test_web_plist_is_a_keepalive_daemon() -> None:
    # The #65 acceptance (auto-start at login + relaunch on exit) hinges on these two keys —
    # dropping either would pass every argv-only test while silently losing the contract.
    data = _load_plist("com.agora.web.plist")
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True


def test_harvest_plist_has_start_interval_schedule() -> None:
    data = _load_plist("com.agora.harvest.plist")
    assert "harvest" in data["ProgramArguments"]
    interval = data["StartInterval"]
    assert isinstance(interval, int) and interval > 0
    # RunAtLoad gives the one documented run right after login/boot; StartInterval alone
    # would silently defer the first harvest by a full interval.
    assert data["RunAtLoad"] is True
    # A periodic short job, not a daemon: KeepAlive would respawn it in a tight loop.
    assert "KeepAlive" not in data


# --- systemd units -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", SERVICE_NAMES)
def test_service_parses_with_execstart(name: str) -> None:
    unit = _load_unit(SYSTEMD_DIR / name)
    assert unit.has_section("Unit") and unit.get("Unit", "Description")
    exec_start = unit.get("Service", "ExecStart")
    assert exec_start.startswith("/"), "ExecStart's first token must be an absolute path"
    assert "uv run --directory " in exec_start
    assert " agora " in exec_start
    assert "--repo" in exec_start


@pytest.mark.parametrize("name", ["agora-watch.service", "agora-web.service"])
def test_daemon_services_restart_on_failure(name: str) -> None:
    unit = _load_unit(SYSTEMD_DIR / name)
    assert unit.get("Service", "Restart") == "on-failure"
    assert unit.get("Install", "WantedBy") == "default.target"


def test_web_service_binds_loopback_only() -> None:
    exec_start = _load_unit(SYSTEMD_DIR / "agora-web.service").get("Service", "ExecStart")
    assert "--host 127.0.0.1" in exec_start
    assert "0.0.0.0" not in exec_start


def test_harvest_service_is_oneshot_for_the_timer() -> None:
    unit = _load_unit(SYSTEMD_DIR / "agora-harvest.service")
    assert unit.get("Service", "Type") == "oneshot"
    assert " harvest" in unit.get("Service", "ExecStart")
    # The timer owns the cadence; a oneshot with Restart= would fight it.
    assert not unit.has_option("Service", "Restart")


def test_harvest_timer_schedule() -> None:
    unit = _load_unit(SYSTEMD_DIR / TIMER_NAME)
    assert unit.get("Timer", "OnBootSec")
    assert unit.get("Timer", "OnUnitActiveSec")
    assert unit.get("Timer", "Unit") == "agora-harvest.service"
    assert unit.get("Install", "WantedBy") == "timers.target"


# --- extras: the web units must be self-sufficient -----------------------------------------------

WEB_EXTRAS = ["web", "ingest", "metrics"]


def test_web_plist_argv_carries_the_web_extras() -> None:
    # Without the extras, a fresh checkout dies on the fastapi import and KeepAlive spins a
    # respawn loop — `uv run --extra` makes the unit install its own deps.
    argv = _load_plist("com.agora.web.plist")["ProgramArguments"]
    extras = [argv[i + 1] for i, a in enumerate(argv) if a == "--extra"]
    assert extras == WEB_EXTRAS, f"web plist must pass the web extras to uv run, got {extras}"


def test_web_service_execstart_carries_the_web_extras() -> None:
    exec_start = _load_unit(SYSTEMD_DIR / "agora-web.service").get("Service", "ExecStart")
    for extra in WEB_EXTRAS:
        assert f"--extra {extra}" in exec_start, f"web service must pass --extra {extra}"


# --- unbuffered stdout (real-time logs under a service manager) ----------------------------------


@pytest.mark.parametrize("name", PLIST_NAMES)
def test_plist_sets_unbuffered_python(name: str) -> None:
    # stdout to a StandardOutPath file is non-tty → CPython block-buffers the watch tick
    # print() lines for hours and drops the buffered tail on SIGTERM/crash.
    env = _load_plist(name)["EnvironmentVariables"]
    assert env["PYTHONUNBUFFERED"] == "1"


@pytest.mark.parametrize("name", SERVICE_NAMES)
def test_service_sets_unbuffered_python(name: str) -> None:
    unit = _load_unit(SYSTEMD_DIR / name)
    assert unit.get("Service", "Environment") == "PYTHONUNBUFFERED=1"


# --- placeholder tokens (substitution-miss detection) --------------------------------------------


@pytest.mark.parametrize("path", ALL_UNIT_PATHS, ids=lambda p: p.name)
def test_placeholder_token_present_in_every_unit_file(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert PATH_PLACEHOLDER in text, (
        f"{path.name} must carry the fixed {PATH_PLACEHOLDER!r} token so the documented "
        f"grep catches a missed substitution"
    )


# The raw-text check above is satisfied by the header comments alone — a real-path
# substitution accidentally committed back would still pass it. Pin the tokens inside the
# PARSED command lines too (each of the three path tokens, so a partial substitution is
# caught as well). The timer keeps only the raw-text check: its token lives in a comment.

EXPECTED_PATH_TOKENS = (
    PATH_PLACEHOLDER + "uv",
    PATH_PLACEHOLDER + "agora-kb",
    PATH_PLACEHOLDER + "knowledge-repo",
)


@pytest.mark.parametrize("name", PLIST_NAMES)
def test_plist_argv_keeps_placeholder_paths(name: str) -> None:
    argv = _load_plist(name)["ProgramArguments"]
    assert argv[0] == EXPECTED_PATH_TOKENS[0], "argv[0] must stay the uv placeholder"
    for token in EXPECTED_PATH_TOKENS[1:]:
        assert token in argv, f"{name} ProgramArguments must keep the {token!r} placeholder"


@pytest.mark.parametrize("name", SERVICE_NAMES)
def test_service_execstart_keeps_placeholder_paths(name: str) -> None:
    exec_start = _load_unit(SYSTEMD_DIR / name).get("Service", "ExecStart")
    for token in EXPECTED_PATH_TOKENS:
        assert token in exec_start, f"{name} ExecStart must keep the {token!r} placeholder"


@pytest.mark.parametrize("name", PLIST_NAMES)
def test_plist_log_paths_use_user_placeholder(name: str) -> None:
    data = _load_plist(name)
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert USER_PLACEHOLDER in data[key], f"{name} {key} must use the {USER_PLACEHOLDER} token"


# --- real syntax check (macOS plutil, no system-state change) ------------------------------------


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil not available (non-macOS)")
@pytest.mark.parametrize("name", PLIST_NAMES)
def test_plutil_lint_accepts_plist(name: str) -> None:
    proc = subprocess.run(
        ["plutil", "-lint", str(LAUNCHD_DIR / name)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, f"plutil -lint failed for {name}: {proc.stdout}{proc.stderr}"
