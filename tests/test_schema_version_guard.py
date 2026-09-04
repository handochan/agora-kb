"""The DESIGN §10 V9 KB schema-version support gate (issue #98).

V9's posture is asymmetric: *new binary on an old repo = read-works / write-warns*, but **old
binary on a new repo = fail-loud**. Before #98 the second half was a SILENT MISREAD — every surface
read ``schema_version`` and nobody compared it against what the running build supports, so a v1
binary opening a future v2 repo would treat it as v1 and let the curator write on top of the
misreading. That damage is done by the build that lacks the guard, which is why the guard has to
ship *before* v2 exists rather than alongside it.

One test per acceptance criterion, named for it, plus the structural meta-test that makes the CLI
wiring un-forgettable (``test_no_future_command_can_silently_skip_the_guard``).

No test here touches a real knowledge repo: an "unsupported repo" is just a directory with a
``_meta/taxonomy.yaml`` declaring a version, which is all the canonical read (ADR-0010 §5.1) needs.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pytest
import yaml

import agora_kb.curator.plan as plan_mod
from agora_kb import cli as cli_mod
from agora_kb.cli import build_parser, main
from agora_kb.config import (
    MAX_SUPPORTED_KB_SCHEMA_VERSION,
    SUPPORTED_KB_SCHEMA_VERSIONS,
    ConfigError,
    ReadOnlySchemaVersionError,
    RepoConfig,
    UnsupportedSchemaVersionError,
    assert_supported_kb_schema_version,
    assert_writable_kb_schema_version,
    guard_repo_schema_version,
    load_repo_config,
    read_kb_schema_version,
)
from agora_kb.core import RepoLayout
from agora_kb.curator.plan import Plan, PlanParseError
from agora_kb.schema import Taxonomy, lint

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

# A version this build cannot possibly support, derived from the constant so the tests keep failing
# for the right reason on the day the supported set is widened to {1, 2}.
_FUTURE = MAX_SUPPORTED_KB_SCHEMA_VERSION + 1


def _repo_at_schema(root: Path, version: int) -> Path:
    """Write the CANONICAL declaration (``_meta/taxonomy.yaml``, ADR-0010 §5.1) at ``version``."""
    meta = root / "_meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "taxonomy.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": version,
                "taxonomy_policy": "open",
                "allowed_tags": {},
                "domains": ["general"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return root


def _write_repo_yaml(root: Path, text: str) -> None:
    kb = root / "_kb"
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "repo.yaml").write_text(text, encoding="utf-8")


def _cfg_at(version: int) -> RepoConfig:
    return RepoConfig(taxonomy=Taxonomy(schema_version=version))


# --- criterion 1: every acting command fails loud, one line, non-zero ---------------------------

# `serve` and `web` are listed by the criterion and are safe to invoke here BECAUSE the guard runs
# at dispatch, before the lazy face import — no fastmcp/fastapi/uvicorn is touched, nothing binds a
# port. That ordering is itself part of what this asserts.
_GUARDED_INVOCATIONS = [
    ["status"],
    ["curate"],
    ["harvest"],
    ["serve"],
    ["web"],
]


@pytest.mark.parametrize("argv", _GUARDED_INVOCATIONS, ids=lambda a: a[0])
def test_criterion_1_acting_commands_fail_loud_on_an_unsupported_repo(
    argv: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A repo declaring a newer schema stops status/curate/harvest/serve/web — cleanly."""
    _repo_at_schema(tmp_path, _FUTURE)

    rc = main([*argv, "--repo", str(tmp_path)])

    captured = capsys.readouterr()
    assert rc == 1
    # A traceback is the failure mode being removed: it buries the one actionable sentence and
    # reads as a crash rather than a refusal.
    assert "Traceback" not in captured.err
    stderr_lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(stderr_lines) == 1, stderr_lines
    assert stderr_lines[0].startswith("agora: ")
    # Nothing was attempted: no status block, no run report, no server banner.
    assert captured.out == ""


def test_criterion_1_the_same_commands_run_normally_on_a_supported_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control: at a SUPPORTED version the guard is invisible (no new failure mode)."""
    _repo_at_schema(tmp_path, MAX_SUPPORTED_KB_SCHEMA_VERSION)

    assert main(["status", "--repo", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "inbox depth: 0" in out


# --- criterion 2: doctor DIAGNOSES the skew instead of dying on it ------------------------------


def test_criterion_2_doctor_reports_the_skew_and_goes_unhealthy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Doctor is the one command exempt from the guard — because explaining the skew is its job.

    (``--skip-probe``: the verdict under test is the schema line's, not a brain daemon's.)
    """
    _repo_at_schema(tmp_path, _FUTURE)

    rc = main(["doctor", "--repo", str(tmp_path), "--skip-probe"])

    out = capsys.readouterr().out
    supported = sorted(SUPPORTED_KB_SCHEMA_VERSIONS)
    assert f"  schema: repo={_FUTURE} supported={supported} (UNSUPPORTED" in out
    # It reached the verdict line — i.e. it did not crash partway — and the verdict moved.
    assert "status: unhealthy" in out
    assert rc == 1


def test_criterion_2_the_schema_line_is_what_moves_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The line CONTRIBUTES (unlike the observational ``_doctor_*`` lines) — asserted at the seam.

    Host-independent on purpose: a full-report comparison would depend on whether *this* machine
    has a kernel sandbox, so the contribution is proven where it is made — the helper's return
    value, which ``_cmd_doctor`` folds into ``ok`` with the same ``and`` form as every other
    verdict contributor.

    Driven from REAL repos on disk rather than a hand-built ``RepoConfig``: the helper takes the
    layout and re-reads the canonical file, which is the whole point of the fix below
    (``test_doctor_reads_the_canonical_file_not_the_defaulted_config``).
    """
    good = _repo_at_schema(tmp_path / "good", MAX_SUPPORTED_KB_SCHEMA_VERSION)
    bad = _repo_at_schema(tmp_path / "bad", _FUTURE)

    assert cli_mod._doctor_schema(RepoLayout(good)) is True
    assert cli_mod._doctor_schema(RepoLayout(bad)) is False
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (
        f"  schema: repo={MAX_SUPPORTED_KB_SCHEMA_VERSION} "
        f"supported={sorted(SUPPORTED_KB_SCHEMA_VERSIONS)}"
    )
    assert "(UNSUPPORTED" in out[1]


def test_doctor_reads_the_canonical_file_not_the_defaulted_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A skewed repo whose ``repo.yaml`` ALSO fails to load must still report the real version.

    ``_doctor_repo_config`` substitutes ``RepoConfig()`` DEFAULTS whenever ``repo.yaml`` raises for
    ANY reason — a type error, a YAML typo, a non-UTF-8 file. Reading the schema off that config
    made doctor print ``repo=1 supported=[1]`` and vote ``healthy`` about a repo whose canonical
    ``_meta/taxonomy.yaml`` says otherwise: asserting a version it never read, on precisely the
    repo this line exists for. Doctor now re-reads the canonical file, like the guard.
    """
    _repo_at_schema(tmp_path, _FUTURE)
    _write_repo_yaml(tmp_path, "name: t\ncurator:\n  max_attempts: not-an-int\n")

    rc = main(["doctor", "--repo", str(tmp_path), "--skip-probe"])

    out = capsys.readouterr().out
    assert "repo.yaml: unreadable" in out  # the config problem is still reported, once
    assert (
        f"  schema: repo={_FUTURE} supported={sorted(SUPPORTED_KB_SCHEMA_VERSIONS)} (UNSUPPORTED"
    ) in out
    assert "status: unhealthy" in out
    assert rc == 1


def test_doctor_says_so_when_the_version_cannot_be_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An indeterminate version is reported as such and FAILS the verdict — "I could not tell"
    is not a pass for the one command whose job is to answer this question."""
    meta = tmp_path / "_meta"
    meta.mkdir(parents=True)
    (meta / "taxonomy.yaml").write_text("schema_version: [not, an, int]\n", encoding="utf-8")

    rc = main(["doctor", "--repo", str(tmp_path), "--skip-probe"])

    out = capsys.readouterr().out
    supported = sorted(SUPPORTED_KB_SCHEMA_VERSIONS)
    assert f"  schema: repo=? supported={supported} (UNREADABLE — cannot verify)" in out
    assert "status: unhealthy" in out
    assert rc == 1


def test_criterion_2_doctor_still_reports_on_a_repo_it_cannot_support(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The diagnostic surface stays WHOLE: every other doctor line is still printed."""
    _repo_at_schema(tmp_path, _FUTURE)

    main(["doctor", "--repo", str(tmp_path), "--skip-probe"])

    out = capsys.readouterr().out
    for line in ("agora doctor (", "  python:", "  harvest:", "  failures:", "status:"):
        assert line in out


# --- criterion 3: the message names version, supported set, and remedy --------------------------


def test_criterion_3_the_failure_message_names_version_supported_and_remedy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """All three facts pinned as strings: an operator must not need a second command to act."""
    _repo_at_schema(tmp_path, _FUTURE)

    assert main(["status", "--repo", str(tmp_path)]) == 1
    message = capsys.readouterr().err.strip()

    # (a) what the repo says, (b) what this build accepts, (c) what to do about it.
    assert f"schema_version {_FUTURE}" in message
    assert f"supported: {sorted(SUPPORTED_KB_SCHEMA_VERSIONS)}" in message
    assert "upgrade agora" in message
    assert "agora repo upgrade" in message  # the #63 repo-side path, named as the other half
    # And WHICH repo refused (a cron line with a stale --repo has to be identifiable).
    assert str(tmp_path) in message
    assert "\n" not in message


def test_criterion_3_the_error_carries_the_facts_as_attributes(tmp_path: Path) -> None:
    """Programmatic callers re-render rather than re-parse the sentence."""
    with pytest.raises(UnsupportedSchemaVersionError) as excinfo:
        assert_supported_kb_schema_version(_cfg_at(_FUTURE), repo=tmp_path)
    assert excinfo.value.version == _FUTURE
    assert excinfo.value.repo == tmp_path


# --- criterion 4: the guard judges the CANONICAL location; lint owns the drift -------------------


def test_criterion_4_guard_judges_on_the_canonical_taxonomy_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``repo.yaml`` says a future version, ``_meta/taxonomy.yaml`` says 1 → the guard PASSES.

    ADR-0010 §5.1 makes ``_meta/taxonomy.yaml`` canonical. Two rules answering the same question
    with different verdicts is worse than either alone, so the guard reads exactly one location and
    leaves the disagreement to the rule that owns it (see the sibling test).
    """
    _repo_at_schema(tmp_path, MAX_SUPPORTED_KB_SCHEMA_VERSION)
    _write_repo_yaml(tmp_path, f"name: r\nschema_version: {_FUTURE}\n")

    # The loader resolved the canonical value, not the mirror...
    assert load_repo_config(RepoLayout(tmp_path)).taxonomy.schema_version == (
        MAX_SUPPORTED_KB_SCHEMA_VERSION
    )
    # ...so the guard is silent and the command runs.
    guard_repo_schema_version(RepoLayout(tmp_path))
    assert main(["status", "--repo", str(tmp_path)]) == 0
    assert capsys.readouterr().err == ""


def test_criterion_4_lint_l1_17_owns_the_cross_location_drift(tmp_path: Path) -> None:
    """The OTHER half of the same repo state: L1-17 is what reports the mirror disagreeing."""
    _repo_at_schema(tmp_path, MAX_SUPPORTED_KB_SCHEMA_VERSION)
    _write_repo_yaml(tmp_path, f"name: r\nschema_version: {_FUTURE}\n")

    result = lint(RepoLayout(tmp_path))

    drift = [f for f in result.findings if f.code == "L1-17"]
    assert len(drift) == 1
    assert drift[0].path == "_kb/repo.yaml"
    assert "canonical" in drift[0].message


def test_criterion_4_the_fallback_still_applies_when_the_canonical_file_is_absent(
    tmp_path: Path,
) -> None:
    """No ``_meta/taxonomy.yaml`` at all ⇒ ``repo.yaml`` IS the resolved value, and is judged.

    This is not an exception to "canonical only": ``_load_taxonomy``'s documented fallback makes
    the mirror the canonical value when the canonical FILE does not exist (a pre-emit repo), and
    the guard judges whatever the loader resolved — one rule, no second parser of its own.
    """
    _write_repo_yaml(tmp_path, f"name: r\nschema_version: {_FUTURE}\n")

    with pytest.raises(UnsupportedSchemaVersionError):
        guard_repo_schema_version(RepoLayout(tmp_path))


# --- criterion 5: old/absent declarations are v1, exactly as before ------------------------------


def test_criterion_5_a_repo_with_no_schema_declaration_is_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Absent taxonomy file, absent key, and empty key all keep reading as 1 — zero regression."""
    # (a) nothing at all — not even an Agora repo. The guard must be SILENT here, not a complaint
    #     about a schema in a directory that has none.
    guard_repo_schema_version(RepoLayout(tmp_path))
    assert main(["status", "--repo", str(tmp_path)]) == 0

    # (b) a taxonomy.yaml with every key EXCEPT schema_version.
    meta = tmp_path / "_meta"
    meta.mkdir()
    (meta / "taxonomy.yaml").write_text(
        "taxonomy_policy: open\nallowed_tags: {}\ndomains: [general]\n", encoding="utf-8"
    )
    assert load_repo_config(RepoLayout(tmp_path)).taxonomy.schema_version == 1
    guard_repo_schema_version(RepoLayout(tmp_path))

    # (c) a repo.yaml with no schema_version either.
    _write_repo_yaml(tmp_path, "name: r\ndomains: [general]\n")
    guard_repo_schema_version(RepoLayout(tmp_path))
    assert main(["status", "--repo", str(tmp_path)]) == 0
    assert capsys.readouterr().err == ""


def test_criterion_5_an_unreadable_config_is_not_the_guards_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed config is SKIPPED, not guessed at — the command reports the real error.

    The guard pre-empting a YAML typo with "unsupported schema" would be a misdiagnosis, and
    ``agora status`` on a broken ``repo.yaml`` must keep behaving exactly as it did.
    """
    _write_repo_yaml(tmp_path, "name: [unclosed\n")

    guard_repo_schema_version(RepoLayout(tmp_path))  # no raise
    assert main(["status", "--repo", str(tmp_path)]) == 0
    assert capsys.readouterr().err == ""


def test_criterion_5_the_loader_never_raises_on_a_skewed_repo(tmp_path: Path) -> None:
    """Fact 6 of the design: the GUARD is an assertion at an entry point, never part of loading.

    A guard inside ``load_repo_config`` would kill ``agora doctor`` on precisely the repo doctor
    exists to diagnose.
    """
    _repo_at_schema(tmp_path, _FUTURE)
    cfg = load_repo_config(RepoLayout(tmp_path))
    assert cfg.taxonomy.schema_version == _FUTURE


# --- criterion 6: a different vocabulary from curator/plan.py's same-shaped constant -------------


def test_criterion_6_the_kb_constant_is_not_the_plan_envelope_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two same-shaped constants, two vocabularies — stated in the docstring, proven by moving them.

    ``curator.plan.SUPPORTED_SCHEMA_VERSIONS`` is the ``plan.json`` ENVELOPE a brain emits
    (ADR-0011 §2 — a property of the curator protocol). ``SUPPORTED_KB_SCHEMA_VERSIONS`` is the KB
    WIKI schema on disk (ADR-0010 §5.1). A v2 wiki schema implies nothing about the plan wire
    format, so widening either set must leave the other exactly where it was.
    """
    # The docstring says so, at the place a reader who confuses them would be standing.
    doc = assert_supported_kb_schema_version.__doc__ or ""
    assert "plan.json" in doc
    assert "curator.plan" in doc

    v2_plan = '{"schema_version": 2, "run_id": "r", "finished": true, "dispositions": []}'

    # The demonstration version is _FUTURE, not a literal 2: the KB set is genuinely {1, 2} now
    # (ADR-0041 D6), so 2 no longer demonstrates anything about independence. The PLAN envelope
    # set is untouched by that widening — which is exactly the property under test — so the plan
    # side keeps using a v2 envelope.
    # Widen the KB set: a _FUTURE REPO is accepted, a v2 PLAN envelope is still refused.
    monkeypatch.setattr(
        "agora_kb.config.SUPPORTED_KB_SCHEMA_VERSIONS",
        frozenset({*SUPPORTED_KB_SCHEMA_VERSIONS, _FUTURE}),
    )
    assert_supported_kb_schema_version(_cfg_at(_FUTURE))
    with pytest.raises(PlanParseError, match="unknown plan schema_version"):
        Plan.from_json(v2_plan)
    monkeypatch.undo()

    # Widen the PLAN set: a v2 envelope parses, a _FUTURE REPO is still refused.
    monkeypatch.setattr(plan_mod, "SUPPORTED_SCHEMA_VERSIONS", frozenset({1, 2}))
    assert Plan.from_json(v2_plan).schema_version == 2
    with pytest.raises(UnsupportedSchemaVersionError):
        assert_supported_kb_schema_version(_cfg_at(_FUTURE))


def test_criterion_6_max_supported_is_derived_not_a_second_source_of_truth() -> None:
    assert MAX_SUPPORTED_KB_SCHEMA_VERSION == max(SUPPORTED_KB_SCHEMA_VERSIONS)
    assert 1 in SUPPORTED_KB_SCHEMA_VERSIONS  # what `agora repo init` writes today


# --- criterion 7: a freshly initialized repo passes --------------------------------------------


@requires_git
def test_criterion_7_a_freshly_initialized_repo_passes_the_guard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``agora repo init`` emits the version this build writes; every command then runs untouched.

    The declared version is :data:`MAX_SUPPORTED_KB_SCHEMA_VERSION` rather than a literal since
    ADR-0041 D6 flipped the default: a fresh repo is initialized at the schema the curator WRITES,
    so "freshly initialized" and "fully usable" stay the same state.
    """
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    capsys.readouterr()

    guard_repo_schema_version(RepoLayout(target))
    assert main(["status", "--repo", str(target)]) == 0
    assert capsys.readouterr().err == ""

    assert main(["doctor", "--repo", str(target), "--skip-probe"]) in (0, 1)
    assert (
        f"  schema: repo={MAX_SUPPORTED_KB_SCHEMA_VERSION} "
        f"supported={sorted(SUPPORTED_KB_SCHEMA_VERSIONS)}"
    ) in capsys.readouterr().out


@requires_git
def test_criterion_7_repo_init_is_exempt_because_it_creates_the_repo(tmp_path: Path) -> None:
    """``repo init`` names its target with a POSITIONAL path, so the guard never sees a repo.

    Deliberate (issue §설계 방향): there is nothing to support-check before the repo exists.
    """
    target = tmp_path / "fresh"
    assert main(["repo", "init", str(target)]) == 0
    assert (
        load_repo_config(RepoLayout(target)).taxonomy.schema_version
        == MAX_SUPPORTED_KB_SCHEMA_VERSION
    )


# --- the faces: the guard is not CLI-only -------------------------------------------------------


def test_build_server_refuses_an_unsupported_repo(tmp_path: Path) -> None:
    """MCP face constructor (a programmatic caller bypasses the CLI dispatch guard entirely)."""
    pytest.importorskip("fastmcp")
    from agora_kb.faces.mcp_server import build_server

    _repo_at_schema(tmp_path, _FUTURE)
    with pytest.raises(UnsupportedSchemaVersionError):
        build_server(repo_path=tmp_path)


def test_build_app_refuses_an_unsupported_repo(tmp_path: Path) -> None:
    """Web face constructor — it owns a WRITE path (upload → Inbox.write), so it must refuse too."""
    pytest.importorskip("fastapi")
    from agora_kb.faces.web.app import build_app

    _repo_at_schema(tmp_path, _FUTURE)
    with pytest.raises(UnsupportedSchemaVersionError):
        build_app(repo_path=tmp_path)


# --- the structural guarantee: no future command can silently skip the guard ---------------------

# Every dispatchable command must EITHER take `--repo` (and is then guarded automatically by
# `main`, with no per-command wiring to forget) or appear here with a reason. A new command that
# names a repo some other way fails this test rather than shipping unguarded — the exact class of
# omission #98 exists to close.
# Commands that take no `--repo` AND declare no positional repo attribute. Each entry is a decision,
# not an oversight — and the list is short on purpose. `repo init` and `import` are NOT here: an
# earlier draft exempted them on the premise that they "CREATE the repo they name", which is false
# (neither refuses an already-initialized destination, and both write `wiki/`/`_meta/` and commit),
# so they now declare `schema_guard_attr` and are guarded on their positional path instead.
_COMMANDS_THAT_NAME_NO_REPO = {
    "repo": "usage stub — `agora repo` with no subcommand touches nothing",
    "index": "usage stub — `agora index` with no subcommand touches nothing",
    "gold": "usage stub — `agora gold` with no subcommand touches nothing",
}

# The one command that takes `--repo` and is still exempt, by design: doctor DIAGNOSES the skew.
_EXEMPT_WITH_REPO = {"doctor"}


def _dispatchable(
    parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
) -> list[tuple[str, argparse.ArgumentParser]]:
    """Every parser argparse can dispatch to (i.e. that carries a ``func`` default), depth-first."""
    found: list[tuple[str, argparse.ArgumentParser]] = []
    if parser.get_default("func") is not None:
        found.append((" ".join(prefix), parser))
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                found.extend(_dispatchable(sub, (*prefix, name)))
    return found


def _takes_repo_option(parser: argparse.ArgumentParser) -> bool:
    return any("--repo" in action.option_strings for action in parser._actions)


def test_no_future_command_can_silently_skip_the_guard() -> None:
    commands = _dispatchable(build_parser())
    assert commands, "the parser tree yielded no commands — the walk is broken"

    for name, parser in commands:
        if name in _COMMANDS_THAT_NAME_NO_REPO:
            # Stale-exemption catch: the moment such a command grows `--repo`, it is guarded and
            # the entry above must go.
            assert not _takes_repo_option(parser), (
                f"`agora {name}` now takes --repo — drop it from _COMMANDS_THAT_NAME_NO_REPO"
            )
            continue
        # Guarded either by `--repo` or by a declared positional attribute. Both routes go
        # through the SAME `_schema_version_guard`, so a command is covered the day it is added
        # as long as it names its repo one of these two ways.
        declared = parser.get_default("schema_guard_attr")
        assert _takes_repo_option(parser) or declared, (
            f"`agora {name}` names its repo neither with --repo nor with a declared "
            f"schema_guard_attr, so `main`'s #98 schema guard cannot see it. Give it --repo, or "
            f'`set_defaults(schema_guard_attr="<dest-arg>")`, or add it to '
            f"_COMMANDS_THAT_NAME_NO_REPO with a reason it acts on no existing repo."
        )
        if declared:
            # The declared attribute must actually be an argument of that parser, or the guard
            # silently reads None and passes — a hole that looks like coverage.
            names = {a.dest for a in parser._actions}
            assert declared in names, (
                f"`agora {name}` declares schema_guard_attr={declared!r} but has no such argument"
            )


def test_only_doctor_opts_out_of_the_guard() -> None:
    """The exemption is a single, greppable parser default — not scattered per-command logic."""
    exempt = {
        name
        for name, parser in _dispatchable(build_parser())
        if parser.get_default("skip_schema_guard") is True
    }
    assert exempt == _EXEMPT_WITH_REPO


def test_version_never_reaches_the_guard(capsys: pytest.CaptureFixture[str]) -> None:
    """``agora --version`` answers "which build?" from inside ``parse_args`` — before dispatch.

    That is what makes it exempt without an entry anywhere: the question the guard's own remedy
    ("upgrade agora") sends the operator to ask must be answerable on a broken repo.
    """
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.startswith("agora ")


# --- the gate must not be switchable off by an unrelated config problem -------------------------


def test_the_gate_survives_an_unrelated_repo_yaml_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unrelated ``repo.yaml`` typo must NOT disable the #98 gate.

    Routing the guard through ``load_repo_config`` and swallowing its exceptions made the whole
    V9 posture conditional on the ENTIRE config parsing — so ``curator.max_attempts: not-an-int``
    let ``agora status`` run happily on a schema-2 repo. Two things make that the wrong coupling:
    an unrelated key has nothing to do with schema support, and "the old binary cannot parse the
    new repo's config" is exactly what a schema bump is most likely to produce, i.e. the gate
    would switch itself off in the situation it exists for. The old docstring justified the
    swallow with "the command's own loader raises the real error a moment later" — ``agora status``
    disproves it: it printed a clean status block and exited 0.
    """
    _repo_at_schema(tmp_path, _FUTURE)
    _write_repo_yaml(tmp_path, "name: t\ncurator:\n  max_attempts: not-an-int\n")

    # Precondition: the full loader really does fail on this repo, so the test is exercising the
    # path it claims to (and not passing because the value was benign).
    with pytest.raises(ConfigError):
        load_repo_config(RepoLayout(tmp_path))

    rc = main(["status", "--repo", str(tmp_path)])

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert f"schema_version {_FUTURE} is not supported" in captured.err


def test_an_indeterminate_version_passes_silently(tmp_path: Path) -> None:
    """Unknown is NOT unsupported: a version that cannot be read must not manufacture a refusal.

    The command that owns the broken file reports it with the context only it has; pre-empting
    that with "unsupported schema" would misdiagnose a YAML typo as a version skew.
    """
    meta = tmp_path / "_meta"
    meta.mkdir(parents=True)
    (meta / "taxonomy.yaml").write_text("schema_version: {not: an-int}\n", encoding="utf-8")

    assert read_kb_schema_version(RepoLayout(tmp_path)) is None
    guard_repo_schema_version(RepoLayout(tmp_path))  # must not raise


def test_a_yaml_11_boolean_is_not_read_as_version_one(tmp_path: Path) -> None:
    """``schema_version: yes`` parses to ``True``, and ``bool`` is an ``int`` subclass in Python.

    Without the explicit bool rejection that would read as version 1 and PASS — a silent misread
    of exactly the kind #98 exists to stop.
    """
    meta = tmp_path / "_meta"
    meta.mkdir(parents=True)
    (meta / "taxonomy.yaml").write_text("schema_version: yes\n", encoding="utf-8")

    assert read_kb_schema_version(RepoLayout(tmp_path)) is None


def test_a_repo_that_names_no_version_is_still_version_one(tmp_path: Path) -> None:
    """Criterion 5: a pre-#98 repo (no ``schema_version`` anywhere) keeps working unchanged."""
    (tmp_path / "_meta").mkdir(parents=True)
    (tmp_path / "_meta" / "taxonomy.yaml").write_text("domains: [general]\n", encoding="utf-8")

    assert read_kb_schema_version(RepoLayout(tmp_path)) == 1
    guard_repo_schema_version(RepoLayout(tmp_path))  # must not raise


# --- the positional writers: `import` / `repo init` onto an EXISTING unsupported repo -----------


@pytest.mark.parametrize(
    "argv_for",
    [
        pytest.param(lambda p: ["import", str(p / "vault"), str(p / "kb")], id="import"),
        pytest.param(lambda p: ["repo", "init", str(p / "kb")], id="repo-init"),
    ],
)
def test_positional_writers_refuse_an_existing_unsupported_repo(
    argv_for, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both were exempt on the premise that they CREATE the repo they name. Neither does.

    ``import`` does not refuse a populated Agora repo — it writes ``wiki/``, ``raw/``, re-emits
    ``_meta/`` and makes its own git commit — and ``repo init`` is idempotent by design, so
    re-running it on an existing repo is a documented flow. Both are therefore DIRECT writers into
    a repo this build may not understand, which is the damage #98 exists to prevent.
    """
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "n.md").write_text("# A note\n", encoding="utf-8")
    _repo_at_schema(tmp_path / "kb", _FUTURE)

    rc = main(argv_for(tmp_path))

    captured = capsys.readouterr()
    assert rc == 1
    assert "Traceback" not in captured.err
    assert f"schema_version {_FUTURE} is not supported" in captured.err


@requires_git
def test_import_into_a_FRESH_destination_is_still_allowed(tmp_path: Path) -> None:
    """The exemption's real intent survives: creating a new repo is not a skew.

    A directory that is not an Agora repo reads as the default version 1, so the guard is silent
    without needing a special case.
    """
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "n.md").write_text("# A note\n", encoding="utf-8")

    assert main(["import", str(tmp_path / "vault"), str(tmp_path / "fresh")]) == 0


# --- the always-on command re-checks, because the repo can change under it -----------------------


def test_watch_re_asserts_the_schema_every_tick(tmp_path: Path, monkeypatch) -> None:
    """``agora watch`` outlives a repo that may change under it (a `git pull` of a newer schema).

    The dispatch guard runs ONCE per process, but ``_watch_tick`` already re-reads the config every
    pass so operator edits take effect without a restart — the schema is part of what may have
    changed, and this is the one always-on command hosting the single writer of ``wiki/``.
    """
    from agora_kb.core import Repo

    _repo_at_schema(tmp_path, MAX_SUPPORTED_KB_SCHEMA_VERSION)
    _write_repo_yaml(tmp_path, "name: t\n")
    repo = Repo(RepoLayout(tmp_path))

    # The repo becomes unsupported AFTER the process started.
    _repo_at_schema(tmp_path, _FUTURE)

    with pytest.raises(UnsupportedSchemaVersionError):
        cli_mod._watch_tick(repo)


# --- ADR-0041 D6: SUPPORT is membership, WRITABILITY is equality with the newest ---------------


def test_schema_1_is_still_supported_after_the_widening() -> None:
    """{1, 2} — never {2} (ADR-0041 D6): dropping 1 would strand the two live schema-1 KBs.

    ``agora repo upgrade`` (#63) does not exist, so a build that refused schema 1 would leave them
    unreadable with no path forward. The set is a SET rather than a ceiling for exactly this case.
    """
    assert SUPPORTED_KB_SCHEMA_VERSIONS == frozenset({1, 2})
    assert MAX_SUPPORTED_KB_SCHEMA_VERSION == 2
    assert_supported_kb_schema_version(_cfg_at(1))
    assert_supported_kb_schema_version(_cfg_at(2))


def test_reads_still_work_on_a_schema_1_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The read half of D6's asymmetry, end to end: an old repo still answers read commands."""
    _repo_at_schema(tmp_path, 1)

    assert main(["status", "--repo", str(tmp_path)]) == 0
    assert capsys.readouterr().err == ""
    guard_repo_schema_version(RepoLayout(tmp_path))  # the entry-point guard stays silent


def test_writable_predicate_accepts_only_the_newest_schema() -> None:
    assert_writable_kb_schema_version(_cfg_at(MAX_SUPPORTED_KB_SCHEMA_VERSION))
    assert_writable_kb_schema_version(MAX_SUPPORTED_KB_SCHEMA_VERSION)  # bare int accepted too


def test_writable_predicate_refuses_a_readable_but_older_schema(tmp_path: Path) -> None:
    """The gap the predicate exists for: supported (readable) is NOT the same as writable.

    With the set widened to {1, 2} every entry-point guard PASSES for a schema-1 repo, so the
    write refusal needs its own per-write-path predicate (ADR-0041 D6).
    """
    guard_repo_schema_version(RepoLayout(_repo_at_schema(tmp_path, 1)))  # supported: silent
    assert_supported_kb_schema_version(_cfg_at(1))  # supported: silent

    with pytest.raises(ReadOnlySchemaVersionError) as excinfo:
        assert_writable_kb_schema_version(_cfg_at(1), repo=tmp_path)

    message = str(excinfo.value)
    assert "agora import --from-kb" in message  # the ONE crossing that exists
    assert "READ-ONLY" in message
    assert str(tmp_path) in message
    assert "\n" not in message  # one line, printed verbatim by the CLI
    assert excinfo.value.version == 1
    assert excinfo.value.repo == tmp_path


def test_read_only_error_is_catchable_as_the_existing_type() -> None:
    """Subclassing keeps every ``except UnsupportedSchemaVersionError`` handler working unchanged.

    …while a caller that wants to distinguish *"cannot read your repo"* from *"will not write your
    repo"* catches the narrower type first.
    """
    assert issubclass(ReadOnlySchemaVersionError, UnsupportedSchemaVersionError)
    assert issubclass(ReadOnlySchemaVersionError, ConfigError)
    with pytest.raises(UnsupportedSchemaVersionError):
        assert_writable_kb_schema_version(_cfg_at(1))


def test_writable_predicate_reports_an_unreadable_schema_as_unsupported_not_read_only(
    tmp_path: Path,
) -> None:
    """A v3 repo gets the "upgrade agora" complaint, NOT "run agora import --from-kb".

    Two failure modes, two messages: telling an operator whose repo this build cannot even read to
    run a converter that does not understand it either would be wrong advice.
    """
    with pytest.raises(UnsupportedSchemaVersionError) as excinfo:
        assert_writable_kb_schema_version(_cfg_at(_FUTURE), repo=tmp_path)

    assert not isinstance(excinfo.value, ReadOnlySchemaVersionError)
    assert "upgrade agora" in str(excinfo.value)
    assert "agora import --from-kb" not in str(excinfo.value)


@pytest.mark.parametrize("bad", [None, "1", 1.0, True, object()])
def test_writable_predicate_rejects_a_non_config_non_int(bad: object) -> None:
    with pytest.raises(TypeError):
        assert_writable_kb_schema_version(bad)  # type: ignore[arg-type]


def test_the_write_refusal_is_wired_at_exactly_the_modules_D6_names() -> None:
    """ADR-0041 D6 names the write-refusal call sites EXHAUSTIVELY; this pins that they are it.

    The list in the ADR: ``agora curate`` / ``watch`` / ``requeue`` in ``cli.py``, ``Inbox.write``
    itself (ONE call covering ``kb_remember``, the web upload route and every future writer, which
    is why it goes there rather than at each face), and the ``kb_curate`` MCP handler. Three
    modules carry those five sites.

    Two directions, and both matter. A module going MISSING is a write path that silently
    corrupts a schema-1 repo. A module APPEARING is a face growing its own copy of the gate —
    which is how two surfaces end up disagreeing about which repos are writable, and how the "one
    call covers every future writer" property quietly stops being true.

    The low-level predicate is checked separately: it must stay confined to the module that
    defines it and the ONE shared wrapper, so nothing re-derives the rule (in particular the
    "declares nothing is UNKNOWN, not schema 1" half) from the raw version.
    """
    src = Path(cli_mod.__file__).resolve().parent

    def modules_mentioning(symbol: str) -> list[str]:
        return sorted(
            path.relative_to(src).as_posix()
            for path in src.rglob("*.py")
            if symbol in path.read_text(encoding="utf-8")
        )

    assert modules_mentioning("assert_writable_repo_schema") == [
        "cli.py",
        "core/inbox.py",
        "faces/mcp_server.py",
    ]
    assert modules_mentioning("assert_writable_kb_schema_version") == [
        "config.py",
        "core/inbox.py",
    ]
