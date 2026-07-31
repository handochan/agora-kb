"""Lock the version anchor (issue #101).

Before #101 the version lived as **two independent hardcoded `0.0.0` literals** — one in
``pyproject.toml``, one in ``src/agora_kb/__init__.py`` — with nothing keeping them equal and
nothing reading either. These tests exist so that hole cannot reopen.

The obvious test ("assert the two agree") is the one that does not work here, and the reason is
worth writing down. The dogfooder's environment is an **editable** install: ``uv sync`` freezes an
``agora_kb-<v>.dist-info`` at sync time, so the moment anyone edits ``__version__`` the metadata
still reports the OLD string until the next sync. A naive equality assertion would therefore go red
in the normal edit-then-test loop for a reason that has nothing to do with the code — the exact
failure mode that trains people to ignore a test.

So the invariant is locked at the level where it is actually total, in three layers:

1. **Structural** (:func:`test_pyproject_derives_the_version_from_the_package`) — assert that
   ``pyproject.toml`` declares ``dynamic = ["version"]`` and sources it from
   ``src/agora_kb/__init__.py``, and that no static ``version =`` survives under ``[project]``.
   This is the strongest form: it asserts there is exactly ONE writable location, so divergence is
   not "unlikely", it is unrepresentable. It needs no install and holds on a bare checkout.
2. **Format** (:func:`test_version_is_canonical_pep440`) — the literal must be PEP 440 *and*
   already canonical, because ``uv build`` names artifacts with the normalized form: a SemVer-ish
   ``0.1.0-beta.1`` would ship as ``agora_kb-0.1.0b1-*`` and skew filenames away from the git tag
   (issue #101 설계 방향).
3. **Observed** (:func:`test_installed_metadata_matches_the_source_of_truth`) — for a real
   (non-editable) install the built metadata MUST equal ``__version__``, and that is asserted hard.
   For an editable install a mismatch is a stale-dist-info artifact, not a defect, so it skips with
   a message naming the fix (``uv sync``). The skip is narrow on purpose: it is gated on PEP 610
   ``direct_url.json`` saying ``editable: true``, never on a bare ``except``. In CI the package is
   synced at HEAD, so the equality assertion really runs there — the skip only ever fires on a
   developer's mid-edit working tree.
"""

from __future__ import annotations

import json
import re
import tomllib
from importlib.metadata import PackageNotFoundError, distribution
from importlib.metadata import version as metadata_version
from pathlib import Path
from typing import Any

import pytest
from packaging.version import InvalidVersion, Version

import agora_kb

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

DIST_NAME = "agora-kb"
# The single writable location, as pyproject must name it (POSIX-separated: it is a TOML string
# consumed by hatchling, not an OS path).
VERSION_SOURCE_PATH = "src/agora_kb/__init__.py"


@pytest.fixture(scope="module")
def pyproject_text() -> str:
    assert PYPROJECT_PATH.is_file(), f"missing pyproject: {PYPROJECT_PATH}"
    return PYPROJECT_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pyproject(pyproject_text: str) -> dict[str, Any]:
    return tomllib.loads(pyproject_text)


def test_pyproject_derives_the_version_from_the_package(pyproject: dict[str, Any]) -> None:
    """There must be exactly ONE place a version can be written, and it is not pyproject.

    ``dynamic = ["version"]`` + ``[tool.hatch.version] path`` is what makes the sdist/wheel names
    and the installed metadata *derivations* of :data:`agora_kb.__version__` rather than a second
    copy of it. Asserting the mechanism (not an equality of two values) is what makes divergence
    unrepresentable instead of merely unlikely — a static ``version =`` reappearing under
    ``[project]`` is precisely the pre-#101 state, and PEP 621 forbids declaring a field both
    statically and dynamically, so it would also break the build.
    """
    project = pyproject["project"]

    assert "version" in project.get("dynamic", []), (
        '[project] must declare `dynamic = ["version"]` so the version is derived from '
        f"{VERSION_SOURCE_PATH} (issue #101)"
    )
    assert "version" not in project, (
        "[project] must NOT carry a static `version = ...` — that is the second hardcoded literal "
        "issue #101 removed; PEP 621 also forbids a field being both static and dynamic"
    )

    hatch_version = pyproject["tool"]["hatch"]["version"]
    assert hatch_version["path"] == VERSION_SOURCE_PATH, (
        f"[tool.hatch.version] must read {VERSION_SOURCE_PATH}; pointing it elsewhere silently "
        "decouples the built artifacts from what `agora --version` prints"
    )


def test_the_version_source_defines_exactly_one_version_literal() -> None:
    """hatchling greps the module for ``__version__``; a second literal would make the build lie.

    The build backend does not import the module, it regex-matches the first line-anchored
    ``__version__ = "..."`` in the file. So "one place to write the version" is only true while the
    file contains one such line — a leftover/duplicate assignment (e.g. under a conditional) would
    have the build and the runtime read different literals from the SAME file, which is the #101
    bug wearing a disguise.
    """
    source = (REPO_ROOT / VERSION_SOURCE_PATH).read_text(encoding="utf-8")
    literals = re.findall(r'^__version__ *= *["\'](.+?)["\']', source, flags=re.MULTILINE)

    assert literals == [agora_kb.__version__], (
        f"{VERSION_SOURCE_PATH} must contain exactly one top-level `__version__` assignment and it "
        f"must be the value the package exposes; found {literals!r}"
    )


def test_version_is_canonical_pep440() -> None:
    """The literal must be PEP 440 *and* already normalized, or tag and filenames drift apart.

    Packaging tools normalize on the way out: a SemVer-shaped ``0.1.0-beta.1`` builds as
    ``agora_kb-0.1.0b1-py3-none-any.whl`` while the git tag would read ``v0.1.0-beta.1``, so the
    artifact a bug report names and the commit it points at stop matching by inspection. Asserting
    ``str(Version(x)) == x`` (not merely "parses") is what forbids that whole class.
    """
    raw = agora_kb.__version__
    try:
        parsed = Version(raw)
    except InvalidVersion as exc:  # pragma: no cover - only reachable on a bad edit
        pytest.fail(f"__version__ = {raw!r} is not a valid PEP 440 version: {exc}")

    assert str(parsed) == raw, (
        f"__version__ = {raw!r} is valid but not canonical; PyPI/`uv build` would normalize it to "
        f"{str(parsed)!r} and the artifact filenames would no longer match the `v{raw}` git tag"
    )


def _is_editable_install() -> bool:
    """True when the installed distribution is a PEP 660 editable pointing at a source tree.

    PEP 610 records install provenance in ``direct_url.json``; ``dir_info.editable`` is the only
    machine-readable "this metadata was frozen at sync time" signal available. Absence of the file
    (a plain wheel install from an index) correctly reads as non-editable.
    """
    try:
        raw = distribution(DIST_NAME).read_text("direct_url.json")
    except PackageNotFoundError:
        return False
    if not raw:
        return False
    try:
        return bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except (json.JSONDecodeError, AttributeError):  # pragma: no cover - malformed metadata
        return False


def test_installed_metadata_matches_the_source_of_truth() -> None:
    """Acceptance criterion 3, with the editable-install caveat made explicit rather than hidden.

    A NON-editable install is the release-shaped case: hatchling stamped ``Version:`` into METADATA
    by reading ``__version__``, so any inequality means the derivation chain is broken (a stray
    static version, a mis-pointed ``[tool.hatch.version] path``) and the test fails hard — this is
    what runs against a built wheel and what protects the release.

    An EDITABLE install is a snapshot: the ``.dist-info`` is written once by ``uv sync`` and is not
    refreshed when the module is edited, so a bump legitimately leaves it stale until the next sync.
    That is reported as a skip with the remedy, never as a failure (a red test with a non-code cause
    is worse than no test) and never as a silent pass (the reason is printed). CI syncs at HEAD, so
    there the two are equal and this assertion is live.
    """
    try:
        installed = metadata_version(DIST_NAME)
    except PackageNotFoundError:
        pytest.skip(
            f"{DIST_NAME} is not installed in this environment (source-checkout run) — "
            "there is no install metadata to compare; the structural lock above still holds"
        )

    source = agora_kb.__version__
    if installed != source and _is_editable_install():
        pytest.skip(
            f"editable install: {DIST_NAME} dist-info was frozen at `uv sync` time and reports "
            f"{installed!r}, while the source of truth is now {source!r}. This is stale metadata, "
            "not divergence — refresh it with `uv sync --all-extras --reinstall-package "
            f"{DIST_NAME}`. A bare `uv sync` will NOT do it: uv sees no pyproject change when only "
            "__version__ moved (which is exactly the shape of a release-promotion commit), and it "
            "would also uninstall pytest/ruff, since `dev` is an extra rather than a dependency "
            "group. (A non-editable install fails here instead of skipping.)"
        )

    assert Version(installed) == Version(source), (
        f"installed metadata {installed!r} != agora_kb.__version__ {source!r} — the build is no "
        f"longer deriving its version from {VERSION_SOURCE_PATH}"
    )
