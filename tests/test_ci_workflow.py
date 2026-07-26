"""Lock the CI workflow definition (issue #100).

``.github/workflows/ci.yml`` is the repo-wide gate, and it is the one file in the tree that no
test run can exercise: a mistake in it does not fail locally, it fails *on GitHub*, often at the
"Set up job" stage before a single step executes. These tests are the cheap stand-in — they parse
the workflow with the same YAML reader the rest of the codebase uses and assert the invariants
that a silent edit would otherwise break:

* every ``uses:`` resolves to a ref that actually exists — in particular ``astral-sh/setup-uv``
  must be pinned to a full commit SHA, because upstream publishes **no** bare major alias past
  ``v7`` (``refs/tags/v8`` / ``refs/tags/v9`` do not exist), so an innocuous-looking ``@v9``
  aborts all three legs before any step runs;
* uv itself is version-pinned, so the ``uv lock --check`` drift gate cannot go red on an
  unrelated PR the day uv bumps the lockfile ``revision``;
* the dependency install uses ``--all-extras`` (not ``--extra dev``), the one thing standing
  between this gate and ~108 tests silently vanishing into ``pytest.importorskip``;
* ``continue-on-error`` exists exactly once and only for Windows (#86 promotes it by deleting
  that single line);
* concurrency cancellation is scoped to pull requests, so consecutive main pushes each keep a
  per-commit conclusion instead of the earlier one landing as ``cancelled``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

# Actions that must be pinned to an immutable 40-hex commit SHA rather than a moving alias.
# setup-uv is here because it stopped publishing bare major tags after v7 — an alias is not just
# mutable there, it is *unresolvable*, which fails the job before any step runs.
SHA_PINNED_ACTIONS = ("astral-sh/setup-uv",)

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
# Branch-ish refs that would silently pull unreviewed action code into a required gate.
BRANCH_REFS = {"main", "master", "HEAD"}

REQUIRED_OS = ["ubuntu-latest", "macos-latest", "windows-latest"]


@pytest.fixture(scope="module")
def workflow_text() -> str:
    assert WORKFLOW_PATH.is_file(), f"missing CI workflow: {WORKFLOW_PATH}"
    return WORKFLOW_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(workflow_text: str) -> dict[str, Any]:
    parsed = yaml.safe_load(workflow_text)
    assert isinstance(parsed, dict), "ci.yml must parse to a mapping"
    return parsed


def _test_job(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict) and "test" in jobs, "ci.yml must define a `test` job"
    job = jobs["test"]
    assert isinstance(job, dict)
    return job


def _uses_refs(job: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``(action, ref)`` pairs for every step that pins a marketplace action."""
    pairs: list[tuple[str, str]] = []
    for step in job["steps"]:
        uses = step.get("uses")
        if not uses:
            continue
        action, _, ref = str(uses).partition("@")
        assert ref, f"`uses: {uses}` must pin a ref"
        pairs.append((action, ref))
    return pairs


def test_workflow_parses_and_defines_a_single_matrix_job(workflow: dict[str, Any]) -> None:
    assert workflow["name"] == "CI"
    assert list(workflow["jobs"]) == ["test"]
    job = _test_job(workflow)
    assert job["strategy"]["matrix"]["os"] == REQUIRED_OS
    # A Windows red must never cancel the two required legs.
    assert job["strategy"]["fail-fast"] is False
    assert isinstance(job["timeout-minutes"], int)


def test_workflow_requests_no_write_permissions(workflow: dict[str, Any]) -> None:
    # The gate reads the repo and consumes no secrets; keep it that way.
    assert workflow["permissions"] == {"contents": "read"}


def test_setup_uv_is_pinned_to_a_commit_sha_not_a_major_alias(workflow: dict[str, Any]) -> None:
    """Regression: `astral-sh/setup-uv@v9` is an unresolvable ref, not a floating alias.

    Upstream publishes bare major tags only through v7 and SHA-pins in its own README. A `@v8`
    or `@v9` alias makes GitHub abort every leg with "unable to find version", which no local
    green can detect and which also destroys the Windows diagnostic log this matrix exists for.
    """
    refs = dict(_uses_refs(_test_job(workflow)))
    for action in SHA_PINNED_ACTIONS:
        assert action in refs, f"expected the workflow to use {action}"
        ref = refs[action]
        assert FULL_SHA.match(ref), (
            f"{action} must be pinned to a full 40-hex commit SHA (got {ref!r}); "
            "upstream publishes no bare major alias past v7, so `@v8`/`@v9` do not resolve"
        )


def test_sha_pinned_actions_carry_a_human_readable_version_comment(workflow_text: str) -> None:
    # A raw SHA is unreviewable without the `# vX.Y.Z` trailer that names what it points at.
    for line in workflow_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("uses:"):
            continue
        if not any(action in stripped for action in SHA_PINNED_ACTIONS):
            continue
        assert re.search(r"#\s*v\d+\.\d+\.\d+", stripped), (
            f"SHA-pinned action needs a `# vX.Y.Z` comment naming the release: {stripped!r}"
        )


def test_no_action_is_pinned_to_a_moving_branch(workflow: dict[str, Any]) -> None:
    for action, ref in _uses_refs(_test_job(workflow)):
        assert ref not in BRANCH_REFS, f"{action} is pinned to branch {ref!r}; use a tag or SHA"


def test_uv_itself_is_version_pinned(workflow: dict[str, Any]) -> None:
    """Regression: an unpinned uv makes the `uv lock --check` gate nondeterministic.

    uv rewrites ``uv.lock``'s ``revision`` as the lock format evolves. With setup-uv installing
    latest, the first release that bumps the revision turns a source-unchanged PR red on both
    required legs and blocks every merge. Pinning couples the uv upgrade to a deliberate re-lock.
    """
    steps = _test_job(workflow)["steps"]
    setup = next(s for s in steps if "astral-sh/setup-uv" in str(s.get("uses", "")))
    version = setup["with"].get("version")
    assert isinstance(version, str) and re.match(r"^\d+\.\d+\.\d+$", version), (
        f"setup-uv must pin an exact uv version (got {version!r})"
    )
    # And CI must prove the oldest interpreter the package claims to support.
    assert setup["with"]["python-version"] == "3.12"


def test_install_uses_all_extras_and_runs_tools_through_uv(workflow: dict[str, Any]) -> None:
    runs = [str(s["run"]) for s in _test_job(workflow)["steps"] if "run" in s]
    joined = "\n".join(runs)

    # Drift detection is its own step: `--frozen` alone does NOT validate the lock (that is
    # `--locked`), so without this the acceptance criterion would be silently unmet.
    assert "uv lock --check" in joined
    sync = next(r for r in runs if r.startswith("uv sync"))
    assert "--all-extras" in sync, (
        "`--extra dev` uninstalls web/ingest/metrics deps, silently skipping ~108 tests"
    )
    assert "--extra dev" not in joined
    assert "--frozen" in sync

    # `uv sync` does not put .venv/bin on PATH; a bare `ruff`/`pytest` would hit the system PATH.
    for tool in ("ruff check .", "ruff format --check .", "pytest"):
        assert any(r.startswith("uv run ") and tool in r for r in runs), (
            f"expected a `uv run ... {tool}` step"
        )
    # -ra prints the skip-reason summary that proves the extras really landed.
    assert any(r.startswith("uv run pytest") and "-ra" in r for r in runs)


def test_linux_leg_installs_bubblewrap_for_the_fail_closed_sandbox(
    workflow: dict[str, Any],
) -> None:
    """The Linux leg must install bwrap, or every curator run fail-closes to ``no_backend``.

    ADR-0013 refuses to run a ``network: none`` backend without a real kernel sandbox. macOS
    runners ship ``sandbox-exec``; ubuntu-latest does **not** ship bubblewrap — which is exactly
    how the first CI run failed on the required Linux leg while macOS passed green.
    """
    steps = _test_job(workflow)["steps"]
    installs = [
        s
        for s in steps
        if "run" in s and "bubblewrap" in str(s["run"]) and str(s.get("if", "")).strip()
    ]
    assert installs, "the Linux leg must install bubblewrap (ADR-0013 fail-closed sandbox)"
    # Guarded so the step never runs on the macOS/Windows legs, where apt-get does not exist.
    assert any("Linux" in str(s["if"]) for s in installs)
    # Must precede the test step, or the install is pointless.
    order = [i for i, s in enumerate(steps) if "run" in s]
    bwrap_at = next(i for i in order if "bubblewrap" in str(steps[i]["run"]))
    pytest_at = next(i for i in order if str(steps[i]["run"]).startswith("uv run pytest"))
    assert bwrap_at < pytest_at


def test_continue_on_error_is_windows_only_and_appears_once(
    workflow: dict[str, Any], workflow_text: str
) -> None:
    # #86 promotes Windows to required by deleting exactly one line; keep that surgical.
    assert workflow_text.count("continue-on-error") == 1
    job = _test_job(workflow)
    assert job["continue-on-error"] == "${{ matrix.os == 'windows-latest' }}"
    # Job level, not step level: a Windows failure at *any* step (including `uv sync`) is absorbed.
    for step in job["steps"]:
        assert "continue-on-error" not in step


def test_concurrency_cancels_only_pull_requests(workflow: dict[str, Any]) -> None:
    """Regression: `cancel-in-progress: true` also cancels main pushes.

    On a push event ``github.head_ref`` is empty, so the group collapses to the ref and two
    quick merges leave the earlier commit's run as ``cancelled`` rather than green — which
    misleads any "last green commit on main" release/rollback judgement.
    """
    concurrency = workflow["concurrency"]
    assert concurrency["group"] == "${{ github.workflow }}-${{ github.head_ref || github.ref }}"
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
