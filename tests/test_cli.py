"""Tests for the ``agora`` CLI (DESIGN/ROADMAP Phase 1).

These exercise the dependency-light argparse front-end over the core API. ``serve`` is never
invoked (it blocks on a stdio loop); we only assert exit codes + captured stdout/stderr substrings.
Commands that touch git (``repo init``, ``doctor`` over a real repo) are skipped if ``git`` is
not on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

import agora_kb.curator.requeue as requeue_mod
from agora_kb import __version__
from agora_kb import cli as cli_mod
from agora_kb.adapters import cli_agent_brain, ollama_brain
from agora_kb.cli import main
from agora_kb.config import (
    ReadOnlySchemaVersionError,
    load_backend_registry,
    load_kb_identity,
)
from agora_kb.core import Inbox, Repo, RepoLayout, StateStore, failed_event_count
from agora_kb.core.ids import is_ulid
from agora_kb.core.inbox import InboxReturn
from agora_kb.core.state import LastFailure
from agora_kb.core.wiki import Wiki
from agora_kb.curator.claim import curator_lock
from agora_kb.curator.isolation import SandboxUnavailable
from agora_kb.curator.worker import RunFailure, RunReport
from agora_kb.schema import lint
from tests.support.kb_builder import NoteSpec, build_kb

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


# A model-free stub curator brain shelled by SubprocessBackend (the Phase-1/2 seam): PASS 1 (cwd =
# bundle dir, candidates.json present) emits a single CREATE_THEME plan to stdout; PASS 2 (cwd =
# worktree) fills every agora:body sentinel region with canned prose. No real model in the loop.
_STUB_BRAIN = """\
import json, re, sys
from pathlib import Path

cwd = Path.cwd()
candidates = cwd / "candidates.json"
START = re.compile(r"<!-- agora:body:start id=(.+?) -->")

if candidates.is_file():
    doc = json.loads(candidates.read_text())
    cands = doc["candidates"]
    c0 = cands[0]
    dispositions = [{
        "candidate_id": c0["candidate_id"],
        "event_ids": [p["event_id"] for p in c0["provenance"]],
        "op": "CREATE_THEME",
        "domain": c0.get("domain") or "ai-tech",
        "basename": "curator-concurrency",
        "title": "Curator concurrency model",
        "summary": "One curator advances the curated branch under a per-repo lock.",
        "status": "active",
        "tags": ["curator", "concurrency"],
        "aliases": [],
        "links": [],
        "needs_prose": True,
        "reason": "New concept.",
    }]
    for c in cands[1:]:
        dispositions.append({
            "candidate_id": c["candidate_id"],
            "event_ids": [p["event_id"] for p in c["provenance"]],
            "op": "DROP", "target_basename": None, "needs_prose": False,
            "reason": "Redundant for this run.",
        })
    print(json.dumps({"schema_version": 1, "run_id": doc["run_id"], "finished": True,
                      "dispositions": dispositions}))
    sys.exit(0)

for note in (cwd / "wiki").rglob("*.md"):
    text = note.read_text()
    out, in_region = [], False
    for line in text.split("\\n"):
        if START.search(line):
            out.append(line)
            out.append("The single curator holds a per-repo flock.")
            in_region = True
            continue
        if "agora:body:end" in line:
            in_region = False
            out.append(line)
            continue
        if in_region:
            continue
        out.append(line)
    note.write_text("\\n".join(out))
sys.exit(0)
"""


def _write_stub_adapters(repo_root: Path) -> Path:
    """Write a stub brain script + an adapters.yaml pointing SubprocessBackend at it."""
    brain = repo_root / "stub_brain.py"
    brain.write_text(_STUB_BRAIN, encoding="utf-8")
    adapters = repo_root / "adapters.yaml"
    # NO `read_roots` on purpose (#115). This spec omits `network:`, so it defaults to `none` and
    # PASS 2 runs inside the REAL kernel sandbox on both supported platforms — with the brain
    # script sitting OUTSIDE the worktree, at the KB repo root, exactly where an operator's brain
    # lives. It is therefore the end-to-end proof that a plain adapters.yaml is enough: read_roots
    # could never have expressed a venv interpreter anyway (every root is realpath-resolved while
    # `execvp` walks the access path through unbound symlink components), which is why the Linux
    # leg published placeholder bodies while reporting success.
    adapters.write_text(
        "backends:\n"
        f"  stub: {{ argv: [{sys.executable!r}, {str(brain)!r}], "
        'cwd: "{worktree}", prompt: stdin }\n'
        "default_backend: stub\n",
        encoding="utf-8",
    )
    return repo_root


# --- repo init ----------------------------------------------------------------------------------
@requires_git
def test_repo_init_initializes_and_prints_sha(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "kb"
    rc = main(["repo", "init", str(target)])
    assert rc == 0
    assert Repo.resolve(target).is_initialized()
    out = capsys.readouterr().out.strip()
    # The init commit sha is printed: a full hex object id (40 for sha-1, 64 for sha-256).
    assert len(out) in (40, 64)
    assert all(ch in "0123456789abcdef" for ch in out)


@requires_git
def test_repo_init_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    first = capsys.readouterr().out.strip()
    assert main(["repo", "init", str(target)]) == 0
    second = capsys.readouterr().out.strip()
    assert first == second  # same commit returned on the second call


@requires_git
def test_repo_init_emits_schema_and_repo_config_and_lints_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`repo init` yields a git repo with an emitted schema, a `_kb/repo.yaml`, and a CLEAN lint."""
    target = tmp_path / "kb"
    argv = ["repo", "init", str(target), "--name", "personal"]
    argv += ["--domain", "ai-tech", "--tag", "curator"]
    rc = main(argv)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert len(out) in (40, 64)  # the admin commit sha

    layout = RepoLayout(target)
    # The schema doc, the fixed taxonomy, and the per-repo config were all emitted.
    assert layout.schema_file.is_file()  # AGENTS.md
    assert (target / "_meta" / "taxonomy.yaml").is_file()
    assert (layout.kb_dir / "repo.yaml").is_file()
    # The freshly-initialized repo lints CLEAN (the schema.lint ok contract for `repo init`).
    assert lint(layout).ok


# --- repo init --schema 2 (ADR-0041 D6; OPT-IN in this wave) ------------------------------------
_V2_KIND_DIRS = ("concepts", "summaries", "notes", "maps", "entities", "people")


@requires_git
def test_repo_init_schema_2_emits_the_v2_seed_and_lints_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`repo init --schema 2` produces a repo that passes the SCHEMA-2 lint with zero findings.

    This is the whole point of the flag: the seed must satisfy the ruleset it declares, or the
    first thing an operator sees on a fresh v2 repo is a red KB it did not cause.
    """
    target = tmp_path / "kb"
    argv = ["repo", "init", str(target), "--schema", "2", "--domain", "ai-tech", "--tag", "arch"]
    assert main(argv) == 0
    assert len(capsys.readouterr().out.strip()) in (40, 64)  # the admin commit sha

    layout = RepoLayout(target)
    # The emitted contract is ADR-0041's, and its header mirrors the canonical taxonomy (L1-17).
    doc = layout.schema_file.read_text(encoding="utf-8")
    assert "KB Wiki Schema v2" in doc
    assert "is **`2`**" in doc
    taxonomy = yaml.safe_load((target / "_meta" / "taxonomy.yaml").read_text(encoding="utf-8"))
    assert taxonomy["schema_version"] == 2
    repo_yaml = yaml.safe_load((layout.kb_dir / "repo.yaml").read_text(encoding="utf-8"))
    assert repo_yaml["schema_version"] == 2  # the MIRROR agrees, or L1-17 fires

    # Zero findings under BOTH the taxonomy-derived ruleset and an explicit schema-2 lint.
    assert lint(layout).findings == ()
    assert lint(layout, schema_version=2).findings == ()


@requires_git
def test_repo_init_schema_2_mints_kb_identity_mirrored_into_the_index(tmp_path: Path) -> None:
    """`_meta/kb.yaml` carries a ULID `kb_id`, and the root map's `kb:` mirrors it (D1.5)."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--schema", "2", "--kind", "team"]) == 0

    identity = load_kb_identity(RepoLayout(target))
    assert identity is not None
    assert is_ulid(identity.kb_id)
    assert identity.name == "kb"
    assert identity.declared_kind == "team"  # ADVISORY; the enforcing kind stays in _kb/repo.yaml

    index = yaml.safe_load((target / "index.md").read_text(encoding="utf-8").split("---\n")[1])
    assert index["kind"] == "index"
    assert index["kb"] == identity.kb_id
    assert index["subjects"] == []
    assert index["children"] == []  # an EMPTY root map: no children, no child bullets
    assert index["type"] == "index"  # the OKF mirror of `kind` (ADR-0041 OD-3), not the authority


@requires_git
def test_repo_init_schema_2_materializes_the_kind_directories(tmp_path: Path) -> None:
    """The six kind directories exist with a .gitkeep, and the tree is fully committed.

    The directory IS the kind under schema 2, so the tree is the schema's own statement of what
    kinds exist — including the two that ship EMPTY (summaries, entities) and the one the curator
    may never create (people). An uncommitted placeholder would leave `repo init` handing back a
    dirty worktree.
    """
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--schema", "2"]) == 0
    for name in _V2_KIND_DIRS:
        assert (target / "wiki" / name / ".gitkeep").is_file()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=target, capture_output=True, text=True, check=True
    )
    assert status.stdout == ""


@requires_git
def test_repo_init_schema_2_writes_per_kind_templates(tmp_path: Path) -> None:
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--schema", "2"]) == 0
    templates = target / "_templates"
    assert (templates / "concept.md").is_file()
    assert (templates / "note.md").is_file()
    assert not (templates / "theme.md").exists()  # the per-TYPE pair belongs to schema 1


@requires_git
def test_repo_init_schema_2_is_idempotent_and_never_remints_kb_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A kb_id is minted ONCE at repo creation and never rewritten (ADR-0041 D1.5).

    Re-minting on a re-init would silently re-identify the knowledge base, orphaning the `kb:`
    stamp already mirrored into every note.
    """
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--schema", "2"]) == 0
    first_sha = capsys.readouterr().out.strip()
    identity = load_kb_identity(RepoLayout(target))
    assert identity is not None

    assert main(["repo", "init", str(target), "--schema", "2"]) == 0
    assert capsys.readouterr().out.strip() == first_sha  # no new curated commit
    again = load_kb_identity(RepoLayout(target))
    assert again is not None
    assert again.kb_id == identity.kb_id


@requires_git
def test_repo_init_defaults_to_schema_2(tmp_path: Path) -> None:
    """A NEW repo gets the version this build WRITES (ADR-0041 D6) — no flag required.

    The default is the write target and not a literal: a repo initialized at anything less is
    read-only the moment it exists (curate/watch/requeue and every capture refuse), which is not a
    default anyone would choose. All three declarations agree — the canonical taxonomy, the
    git-ignored mirror, and the emitted schema doc header — so L1-17 finds no drift.
    """
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--domain", "ai-tech"]) == 0

    layout = RepoLayout(target)
    assert "KB Wiki Schema v2" in layout.schema_file.read_text(encoding="utf-8")
    assert (
        yaml.safe_load((target / "_meta" / "taxonomy.yaml").read_text(encoding="utf-8"))[
            "schema_version"
        ]
        == 2
    )
    assert (
        yaml.safe_load((layout.kb_dir / "repo.yaml").read_text(encoding="utf-8"))["schema_version"]
        == 2
    )
    # The whole schema-2 skeleton, on the DEFAULT path now.
    identity = load_kb_identity(layout)
    assert identity is not None
    for name in _V2_KIND_DIRS:
        assert (target / "wiki" / name / ".gitkeep").is_file()
    assert (target / "_templates" / "concept.md").is_file()
    assert not (target / "_templates" / "theme.md").exists()
    index = (target / "index.md").read_text(encoding="utf-8")
    assert "kind: index" in index
    assert f"kb: {identity.kb_id}" in index
    assert lint(layout).findings == ()


@requires_git
def test_repo_init_schema_1_still_works_and_says_it_is_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--schema 1` is still buildable and still lints — and the operator is TOLD what it costs.

    The inverse of the note this replaces. A schema-1 repo is readable (query/status/browse/doctor)
    but READ-ONLY for this build (ADR-0041 D6), and the operator must hear that at init rather than
    discover it at the first refused capture.
    """
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--schema", "1", "--domain", "ai-tech"]) == 0

    err = capsys.readouterr().err
    assert "READ-ONLY" in err
    assert "import --from-kb" in err
    layout = RepoLayout(target)
    assert "KB Wiki Schema v1" in layout.schema_file.read_text(encoding="utf-8")
    assert not (target / "_meta" / "kb.yaml").exists()
    assert not (target / "wiki").exists()
    assert (target / "_templates" / "theme.md").is_file()
    index = (target / "index.md").read_text(encoding="utf-8")
    assert "type: index" in index and "kind: index" not in index
    assert lint(layout).findings == ()


@requires_git
def test_repo_init_refuses_to_convert_an_existing_schema_1_repo_and_writes_NOTHING(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR-0041 D6: "there is no in-place migrator", and no command silently upgrades a repo.

    The regression this pins is a HALF-migration: seeding the schema-2 skeleton and re-stamping the
    git-ignored `_kb/repo.yaml` mirror to 2 over a canonical `_meta/taxonomy.yaml` that stays at 1
    leaves a previously healthy v1 repo permanently L1-17-broken, with the failure arriving only at
    the lint gate AFTER the writes. The refusal must therefore come BEFORE anything is written.
    """
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--schema", "1", "--domain", "general"]) == 0
    capsys.readouterr()
    before = sorted(q.relative_to(target).as_posix() for q in target.rglob("*") if q.is_file())

    assert main(["repo", "init", str(target), "--schema", "2", "--domain", "general"]) == 1
    err = capsys.readouterr().err
    assert "no in-place migrator" in err
    assert "import --from-kb" in err

    # Nothing was written: same files, same mirror, and the repo still lints clean.
    assert sorted(q.relative_to(target).as_posix() for q in target.rglob("*") if q.is_file()) == (
        before
    )
    layout = RepoLayout(target)
    assert not (target / "_meta" / "kb.yaml").exists()
    assert not (target / "wiki").exists()
    assert (
        yaml.safe_load((layout.kb_dir / "repo.yaml").read_text(encoding="utf-8"))["schema_version"]
        == 1
    )
    assert lint(layout).ok


@requires_git
def test_repo_init_refuses_to_downgrade_an_existing_schema_2_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal is symmetric — a conversion in either direction is a re-import, not an edit."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--schema", "2"]) == 0
    capsys.readouterr()

    assert main(["repo", "init", str(target), "--schema", "1"]) == 1
    assert "import --from-kb" in capsys.readouterr().err
    assert lint(RepoLayout(target)).ok


@requires_git
def test_repo_init_re_init_without_the_flag_KEEPS_the_repos_own_schema_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bare re-init is IDEMPOTENT (this command's own docstring) on a schema-2 repo too.

    The regression: `--schema` defaulting to 1 made a flagless re-init rewrite the `_kb/repo.yaml`
    MIRROR to 1 while the canonical `_meta/taxonomy.yaml` kept saying 2 — permanent L1-17 drift
    that fails every later lint and therefore the curator's §4.4 gate, clearable only by hand.
    """
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--schema", "2"]) == 0
    first_sha = capsys.readouterr().out.strip()

    assert main(["repo", "init", str(target)]) == 0  # no --schema at all
    assert capsys.readouterr().out.strip() == first_sha

    layout = RepoLayout(target)
    mirror = yaml.safe_load((layout.kb_dir / "repo.yaml").read_text(encoding="utf-8"))
    canonical = yaml.safe_load((target / "_meta" / "taxonomy.yaml").read_text(encoding="utf-8"))
    assert mirror["schema_version"] == canonical["schema_version"] == 2
    assert lint(layout).ok  # no L1-17 drift


@requires_git
def test_repo_init_refuses_a_declaration_that_runs_AHEAD_of_the_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hand-bumped `_meta/taxonomy.yaml` must NOT make a flagless re-init seed a v2 skeleton.

    "Edit the declared version" is the first thing an operator reaches for when asking how to
    migrate (ADR-0041 D6 answers "you don't"). Resolving the version from the declaration is
    right, but SEEDING schema 2 into the schema-1 tree underneath it is the same half-migration
    the `--schema` contradiction refusal exists to prevent: it leaves `_meta/kb.yaml`, a v2 root
    map and six `wiki/<kind>/` directories as untracked residue behind a failed lint gate, on this
    run and on every re-run. A repo we refuse gets ZERO writes.
    """
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--schema", "1"]) == 0  # a plain schema-1 repo
    capsys.readouterr()
    taxonomy_path = target / "_meta" / "taxonomy.yaml"
    taxonomy = yaml.safe_load(taxonomy_path.read_text(encoding="utf-8"))
    taxonomy["schema_version"] = 2
    taxonomy_path.write_text(yaml.safe_dump(taxonomy, sort_keys=False), encoding="utf-8")

    assert main(["repo", "init", str(target)]) == 1  # a refusal, not a warning
    err = capsys.readouterr().err
    assert "import --from-kb" in err  # the D6 remedy, not a list of lint codes

    # No residue: nothing the schema-2 seed would have written exists.
    assert not (target / "_meta" / "kb.yaml").exists()
    assert not (target / "wiki" / "maps").exists()
    assert not (target / "wiki" / "concepts").exists()


@requires_git
def test_repo_init_refuses_to_adopt_an_UNDECLARED_schema_1_TREE_at_the_new_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An UNPARSEABLE taxonomy declares nothing — and the tree underneath it is still schema 1.

    `read_canonical_kb_schema_version` returns `None` both for a merely git-initialized directory
    AND for a schema-1 repo whose `_meta/taxonomy.yaml` is missing/corrupt. Keying the guard on the
    DECLARATION alone would adopt the second case at the flipped default 2: `_meta/kb.yaml` + six
    kind directories minted over schema-1 notes that `Repo.init` (early-returning) never replaces,
    leaving a repo that DECLARES 2 and fails the v2 lint gate on every curate — stickily, since
    `_meta/kb.yaml` now exists. So the guard reads the TREE's own witness.
    """
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--schema", "1"]) == 0
    capsys.readouterr()
    (target / "_meta" / "taxonomy.yaml").write_text("domains: [oops\n", encoding="utf-8")

    assert main(["repo", "init", str(target)]) == 1  # a refusal, not a silent adoption
    err = capsys.readouterr().err
    assert "import --from-kb" in err  # the D6 remedy
    assert "--schema 1" in err  # ...and the escape hatch for a tree that IS schema 1
    # ZERO writes: none of the schema-2 seed exists.
    assert not (target / "_meta" / "kb.yaml").exists()
    assert not (target / "wiki" / "concepts").exists()

    # The named escape hatch works: no declaration means no contradiction refusal, and schema 1
    # does not trip the guard, so the repo re-emits the layout it is actually on.
    assert main(["repo", "init", str(target), "--schema", "1"]) == 0
    capsys.readouterr()
    assert not (target / "_meta" / "kb.yaml").exists()


@requires_git
def test_repo_init_adopts_an_EMPTY_initialized_directory_at_the_default(tmp_path: Path) -> None:
    """The other side of the same guard: an initialized directory with NO KB content is a FIRST
    init, so it takes the default (schema 2) rather than a refusal whose message would be false
    about a tree that was never built at either version."""
    target = tmp_path / "kb"
    target.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "init", "-b", "main", str(target)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(target), "commit", "--allow-empty", "-m", "empty"],
        check=True,
        capture_output=True,
        env=env,
    )

    assert main(["repo", "init", str(target), "--domain", "ai-tech"]) == 0
    layout = RepoLayout(target)
    assert load_kb_identity(layout) is not None
    for name in _V2_KIND_DIRS:
        assert (target / "wiki" / name / ".gitkeep").is_file()


@requires_git
def test_repo_init_never_lets_the_git_ignored_mirror_decide_the_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_kb/repo.yaml` is git-ignored, operator-local and rewritten HERE — never the authority.

    A pre-#98-shaped repo (canonical taxonomy with no `schema_version` key) plus a mirror
    hand-edited to 2 must still resolve to the canonical default 1: an untracked local edit may not
    reshape a shared, git-tracked tree.
    """
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--schema", "1"]) == 0
    capsys.readouterr()
    layout = RepoLayout(target)

    taxonomy_path = target / "_meta" / "taxonomy.yaml"
    taxonomy = yaml.safe_load(taxonomy_path.read_text(encoding="utf-8"))
    taxonomy.pop("schema_version", None)
    taxonomy_path.write_text(yaml.safe_dump(taxonomy, sort_keys=False), encoding="utf-8")
    mirror_path = layout.kb_dir / "repo.yaml"
    mirror = yaml.safe_load(mirror_path.read_text(encoding="utf-8"))
    mirror["schema_version"] = 2
    mirror_path.write_text(yaml.safe_dump(mirror, sort_keys=False), encoding="utf-8")

    assert main(["repo", "init", str(target)]) == 0
    capsys.readouterr()
    assert not (target / "_meta" / "kb.yaml").exists()
    assert not (target / "wiki" / "maps").exists()
    # The mirror is re-stamped from the CANONICAL resolution, not from its own stale value.
    assert yaml.safe_load(mirror_path.read_text(encoding="utf-8"))["schema_version"] == 1
    # ...and it never gets to veto an explicit --schema either: the canonical file says 1.
    assert main(["repo", "init", str(target), "--schema", "2"]) == 1
    assert "schema-1 KB" in capsys.readouterr().err


@requires_git
def test_repo_init_schema_2_says_nothing_about_being_opt_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Schema 2 is the ordinary case now and needs no announcement (ADR-0041 D6).

    The note it replaces said the opposite — that schema 2 was opt-in and not curate-able — and a
    stale reassurance is worse than none: an operator who reads it would not trust the very default
    the flip just made correct.
    """
    assert main(["repo", "init", str(tmp_path / "kb"), "--schema", "2"]) == 0
    err = capsys.readouterr().err
    assert "not yet curate-able" not in err
    assert "READ-ONLY" not in err


@requires_git
@pytest.mark.parametrize("reserved", ["_blob", "_pages"])
def test_repo_init_refuses_a_reserved_underscore_domain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], reserved: str
) -> None:
    """The tool must never mint a repo whose taxonomy a later load refuses (ADR-0041 D1.4/L1-23).

    `raw/<domain>/` and `raw/_blob/` share one namespace, so the reservation is enforced at the
    DECLARATION site as well as at load and by lint — before anything is written.
    """
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--domain", reserved]) == 1
    assert reserved in capsys.readouterr().err
    assert not (target / "_meta").exists()


@requires_git
def test_curate_reports_an_invalid_config_as_one_line_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A config refusal on the write path is an operator message, like every other command's."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    capsys.readouterr()
    (RepoLayout(target).kb_dir / "repo.yaml").write_text("name: [broken\n", encoding="utf-8")

    assert main(["curate", "--repo", str(target)]) == 1
    captured = capsys.readouterr()
    assert "curate: invalid config" in captured.err
    assert "Traceback" not in captured.err


@requires_git
def test_curate_renders_a_schema_refusal_as_itself_not_as_an_invalid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ReadOnlySchemaVersionError` is a `ConfigError` — the arms must be ORDERED, as elsewhere.

    W2.2 wires `assert_writable_kb_schema_version` into exactly this command. Caught by the blanket
    `except ConfigError`, "this KB is READ-ONLY for this agora build" would print as
    `invalid config: ...` and send the operator to edit a repo.yaml that is perfectly fine.
    """
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    capsys.readouterr()

    def _refuse(layout: RepoLayout) -> None:
        raise ReadOnlySchemaVersionError(1, repo=layout.root)

    monkeypatch.setattr(cli_mod, "load_repo_config", _refuse)

    assert main(["curate", "--repo", str(target)]) == 1
    err = capsys.readouterr().err
    assert "invalid config" not in err
    assert "Traceback" not in err
    assert "READ-ONLY" in err.upper()


def test_repo_init_rejects_an_unknown_schema_version(capsys: pytest.CaptureFixture[str]) -> None:
    """argparse gates the version at the boundary — 3 is not a schema this build can emit."""
    with pytest.raises(SystemExit) as exc:
        main(["repo", "init", "unused", "--schema", "3"])
    assert exc.value.code == 2
    assert "--schema" in capsys.readouterr().err


def test_repo_without_subcommand_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["repo"])
    assert rc == 2
    assert "subcommand" in capsys.readouterr().err


# --- status -------------------------------------------------------------------------------------
def test_status_prints_zero_depth_on_fresh_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["status", "--repo", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "inbox depth: 0" in out
    assert "last_run: never" in out
    assert "ingested=0" in out


def test_status_reflects_a_pending_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = RepoLayout(tmp_path)
    Inbox(layout).write(text="remember this", writer="tester", source="manual")
    rc = main(["status", "--repo", str(tmp_path)])
    assert rc == 0
    assert "inbox depth: 1" in capsys.readouterr().out


# --- status: the #96 failure surface -------------------------------------------------------------
def _plant_failure(
    layout: RepoLayout,
    *,
    when: datetime = datetime(2026, 6, 13, 3, 0, 12, tzinfo=UTC),
    run_id: str = "2026-06-13T03-00-12.000Z--3f2a1b",
    last_run: datetime | None = None,
    last_attempt: datetime | None = None,
) -> None:
    """Write a ``state.json`` carrying ONE recorded curator failure (the criterion-7 shape).

    Planted rather than driven through a real run so the rendering tests can byte-lock a FIXED
    timestamp and run id; the end-to-end path that actually produces this state is covered by
    ``test_status_surfaces_a_non_terminal_failure_end_to_end``.
    """
    store = StateStore(layout)
    state = store.load()
    state.last_attempt = last_attempt if last_attempt is not None else when
    state.last_run = last_run
    state.last_failure = LastFailure.from_run_failure(
        when=when,
        run_id=run_id,
        phase="claimed",
        reasons=["TAXONOMY: unknown domain 'not-a-real-domain'"],
        record_path=f"_kb/failed/2026-06-13/{run_id}/error.json",
    )
    store.save(state)


def test_status_fresh_repo_shows_never_and_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole fresh-repo report, byte-for-byte — the three #96 lines in their empty form."""
    layout = RepoLayout(tmp_path)

    assert main(["status", "--repo", str(tmp_path)]) == 0

    assert capsys.readouterr().out.splitlines() == [
        f"repo: {layout.root}",
        "inbox depth: 0",
        "last_run: never",
        "last_commit: -",
        "counters: ingested=0 merged=0 dropped=0 failed=0",
        "last_attempt: never",
        "last_failure: none",
        "failed_events: 0",
    ]


def test_status_first_five_lines_are_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#96 crit 9 (CLI half): the new lines are APPENDED — the pre-#96 five are byte-identical.

    Driven with a planted failure on purpose: the regression this guards is a rendering change
    that reorders or reformats the existing report when the new fields have CONTENT, which an
    empty-state assertion would never catch. Operators and scripts parse those five lines.
    """
    layout = RepoLayout(tmp_path)
    Inbox(layout).write(text="remember this", writer="tester", source="manual")
    _plant_failure(layout)

    assert main(["status", "--repo", str(tmp_path)]) == 0

    assert capsys.readouterr().out.splitlines()[:5] == [
        f"repo: {layout.root}",
        "inbox depth: 1",
        "last_run: never",
        "last_commit: -",
        "counters: ingested=0 merged=0 dropped=0 failed=0",
    ]


def test_status_marks_a_superseded_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``last_failure`` is STICKY, so the VERDICT WORD is what separates broken from recovered.

    Both renderings are locked in one test against the SAME planted failure: the only delta a
    later successful publish may make is ``UNRESOLVED`` → ``superseded``. Anything else (a
    disappearing line, a reformatted tail) would break `agora status | grep UNRESOLVED`.
    """
    layout = RepoLayout(tmp_path)
    tail = (
        "2026-06-13T03:00:12Z run=2026-06-13T03-00-12.000Z--3f2a1b phase=claimed reasons=1 "
        "record=_kb/failed/2026-06-13/2026-06-13T03-00-12.000Z--3f2a1b/error.json "
        "first=TAXONOMY: unknown domain 'not-a-real-domain'"
    )

    _plant_failure(layout)  # never published → the failure is the current state of the repo
    assert main(["status", "--repo", str(tmp_path)]) == 0
    assert f"last_failure: UNRESOLVED {tail}" in capsys.readouterr().out

    # A later successful publish supersedes it, but does NOT erase it (it is a historical fact).
    _plant_failure(layout, last_run=datetime(2026, 6, 13, 4, 0, 0, tzinfo=UTC))
    assert main(["status", "--repo", str(tmp_path)]) == 0
    assert f"last_failure: superseded {tail}" in capsys.readouterr().out


def test_status_prints_failed_events(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """#96 crit 8 (CLI half): the TERMINAL-failure backlog, via the ONE shared helper.

    The worker nests terminal failures at ``failed/<date>/<run-id>/<event>.md`` with an
    ``error.json`` alongside, so the count must be recursive and must not count the record.
    Asserting against ``failed_event_count`` itself is the "same helper as kb_status" lock.
    """
    layout = RepoLayout(tmp_path)
    run_dir = layout.failed_dir / "2026-06-13" / "2026-06-13T03-00-12.000Z--3f2a1b"
    run_dir.mkdir(parents=True)
    (run_dir / "a.md").write_text("event a", encoding="utf-8")
    (run_dir / "b.md").write_text("event b", encoding="utf-8")
    (run_dir / "error.json").write_text("{}", encoding="utf-8")

    assert main(["status", "--repo", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "failed_events: 2" in out
    assert f"failed_events: {failed_event_count(layout)}" in out


def test_status_failed_events_calls_the_shared_helper(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#96 crit 8 is a "same HELPER" claim, and value equality cannot prove one.

    A re-inlined copy of the same recursive glob would keep every count assertion green. Forcing
    the shared function to an impossible value is what makes "by construction, not by two copies"
    testable: if the CLI ever grows its own glob again, this fails.
    """
    monkeypatch.setattr(cli_mod, "failed_event_count", lambda layout: 99)

    assert main(["status", "--repo", str(tmp_path)]) == 0
    assert "failed_events: 99" in capsys.readouterr().out


def test_status_on_corrupt_state_json_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corrupt ``state.json`` is REPORTED, not tracebacked — #96's own surface must not die on
    #97's headline trigger. The rc is unchanged (an uncaught exception already exited 1)."""
    layout = RepoLayout(tmp_path)
    layout.state_file.parent.mkdir(parents=True, exist_ok=True)
    layout.state_file.write_text("not json at all", encoding="utf-8")

    rc = main(["status", "--repo", str(tmp_path)])

    captured = capsys.readouterr()
    assert rc == 1
    assert "Traceback" not in captured.err
    assert "agora status: _kb/state.json is unreadable — " in captured.err
    assert "1 validation error for CuratorState" in captured.err
    # ONE bounded line: the pydantic message is multi-line and would otherwise flood the report.
    assert captured.err.rstrip("\n").count("\n") == 0
    # The two facts that need no state.json are still reported.
    assert f"repo: {layout.root}" in captured.out
    assert "inbox depth: 0" in captured.out


def test_status_on_a_non_utf8_state_json_is_also_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``StateStore.load`` READS before it validates, so a truncated/half-written/FS-corrupted file
    raises ``UnicodeDecodeError`` — not ``ValidationError``. `agora doctor` and `agora watch` both
    report that shape cleanly; `agora status`, the command #96 criterion 7 routes the operator to,
    must not be the one surface that still tracebacks on it."""
    layout = RepoLayout(tmp_path)
    layout.state_file.parent.mkdir(parents=True, exist_ok=True)
    layout.state_file.write_bytes(b"\xff\xfe\x00binary garbage")

    rc = main(["status", "--repo", str(tmp_path)])

    captured = capsys.readouterr()
    assert rc == 1
    assert "Traceback" not in captured.err
    assert "agora status: _kb/state.json is unreadable — " in captured.err
    assert "codec can't decode byte" in captured.err
    assert f"repo: {layout.root}" in captured.out


# --- ADR-0041 D6: the write refusal at the three CLI surfaces ------------------------------------


def _init_schema_1_repo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """A real, lint-clean schema-1 repo — the owner's existing KBs, which must stay READABLE."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--schema", "1", "--domain", "ai-tech"]) == 0
    capsys.readouterr()
    return target


@requires_git
@pytest.mark.parametrize(
    "argv",
    [
        ["curate", "--force"],
        ["watch", "--once"],
        ["requeue", "--all"],
    ],
    ids=["curate", "watch", "requeue"],
)
def test_a_write_command_refuses_a_schema_1_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """ADR-0041 D6: curate / watch / requeue REFUSE, rather than mixing two layouts in one repo.

    V9's "new binary on an old repo = read-works / write-WARNS" is HARDENED to a refusal for this
    one bump, because the write here is not merely suboptimal but corrupting: APPLY would put
    schema-2 paths and schema-2 frontmatter into a schema-1 tree, producing a repo that is neither,
    that no lint ruleset can gate, and whose damage is a commit.

    All three exits are non-zero and all three messages carry the ONE crossing that exists. Note
    that ``requeue`` is here although it never calls ``Inbox.write``: it is a location-only
    ``os.replace`` back into the inbox (#99), so it would otherwise refill a queue this build's
    curator refuses to drain.
    """
    target = _init_schema_1_repo(tmp_path, capsys)

    rc = main([*argv, "--repo", str(target)])

    assert rc == 1
    combined = capsys.readouterr()
    message = combined.err + combined.out
    assert "READ-ONLY for this agora build" in message
    assert "agora import --from-kb" in message
    assert "Traceback" not in message


@requires_git
def test_reads_keep_working_on_the_repo_the_writes_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of D6, and the reason ``SUPPORTED_KB_SCHEMA_VERSIONS`` stays ``{1, 2}``.

    A build that dropped 1 would STRAND the owner's two live KBs. Reads are what make the refusal
    survivable: the operator can still see the repo they are being told to convert.

    The reads SUCCEED (rc 0 for status and index build) — that is the property. They are no longer
    silent about the posture, though: ``status`` and ``doctor`` each say the repo is read-only,
    which is the second half of the same contract and is asserted in its own tests below.
    """
    target = _init_schema_1_repo(tmp_path, capsys)

    assert main(["status", "--repo", str(target)]) == 0
    assert main(["doctor", "--repo", str(target), "--skip-probe"]) in (0, 1)
    assert main(["index", "build", "--repo", str(target)]) == 0


@requires_git
def test_status_reports_the_read_only_schema_with_a_value_line_and_a_remedy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`agora status` is where an operator looks when captures "are not arriving" (ADR-0041 D6).

    On a schema-1 repo they never will, so status has to say so. Two channels, deliberately: the
    STDOUT line keeps status's ``key: <machine-readable value>`` grammar (a `grep '^schema:'`
    answers "can this be written?"), and the REMEDY — a sentence with no value slot — goes to
    stderr, exactly where ``repo init`` puts the same sentence.
    """
    target = _init_schema_1_repo(tmp_path, capsys)

    assert main(["status", "--repo", str(target)]) == 0

    captured = capsys.readouterr()
    assert "schema: 1 (READ-ONLY — writes refuse)" in captured.out
    assert "READ-ONLY" in captured.err
    assert f"agora import --from-kb {target} <new-repo>" in captured.err


@requires_git
def test_status_on_a_writable_repo_prints_no_schema_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The read-only line must not become noise on every run of the ordinary case."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--domain", "general"]) == 0
    capsys.readouterr()

    assert main(["status", "--repo", str(target)]) == 0

    captured = capsys.readouterr()
    assert "schema:" not in captured.out
    assert "READ-ONLY" not in captured.out + captured.err


@requires_git
def test_doctor_reports_a_read_only_repo_as_unhealthy_with_the_one_crossing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Doctor answers the question the other commands refuse on — including "can I WRITE it?".

    ``SUPPORTED_KB_SCHEMA_VERSIONS`` stays ``{1, 2}`` so reads keep working, which makes
    ``schema: repo=1 supported=[1, 2]`` a PASSING line over a repo whose curator can never run
    again. The ``write:`` line is the missing half, and it FAILS the verdict on the same judgement
    ``test_doctor_offers_requeue_...`` already pins for an unrunnable curator: reporting
    ``healthy`` for a repo that cannot curate is issue #96's opening complaint, and a launchd
    health check that greens on a frozen hub hides it indefinitely.
    """
    target = _init_schema_1_repo(tmp_path, capsys)

    rc = main(["doctor", "--repo", str(target), "--skip-probe"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "  schema: repo=1 supported=[1, 2]" in out
    assert "write: READ-ONLY" in out
    assert "reads KB schema 1 and refuses to write it" in out
    assert f"'agora import --from-kb {target} <new-repo>'" in out
    assert "status: unhealthy" in out


@requires_git
def test_doctor_says_nothing_about_writability_on_a_schema_2_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ordinary case is silent, and a NON-repo directory is silent too.

    The second half is the reason the line is keyed on the CANONICAL declaration: the broader
    reader ``doctor``'s ``schema:`` line uses defaults a bare directory to ``1``, so a banner keyed
    on it would tell every operator running ``agora doctor`` in the wrong cwd that their writes
    are refused.
    """
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--domain", "general"]) == 0
    capsys.readouterr()

    main(["doctor", "--repo", str(target), "--skip-probe"])
    assert "write: READ-ONLY" not in capsys.readouterr().out

    main(["doctor", "--repo", str(tmp_path / "not-a-repo"), "--skip-probe"])
    assert "write: READ-ONLY" not in capsys.readouterr().out


# --- curate -------------------------------------------------------------------------------------
def test_curate_on_empty_repo_should_not_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["curate", "--repo", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "should_run: False" in out
    assert "reason: none" in out
    # Not due => no backend is even loaded; nothing was changed.
    assert "nothing was changed" in out


@requires_git
def test_curate_force_without_backend_reports_no_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A forced curate over a repo with NO adapters.yaml is a clear error (rc=1), not a crash."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--domain", "ai-tech"]) == 0
    capsys.readouterr()
    # `repo init` now emits a default adapters.yaml (the OSS brain wiring); remove it to exercise
    # the explicit "no backend configured" path this test guards.
    (target / "adapters.yaml").unlink()

    rc = main(["curate", "--repo", str(target), "--force"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no backend configured" in err


@requires_git
def test_doctor_prints_the_routing_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR-0015: `agora doctor` reports which brain runs each act + its network posture. A fresh
    repo emits one `qwen` brain with no routing, so both acts resolve to it."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--domain", "ai-tech"]) == 0
    capsys.readouterr()

    # --skip-probe: this test is about the routing TABLE, not brain reachability — without it the
    # fresh repo's `agora-ollama-brain` backend would make a live loopback call (#96 §6.8).
    main(["doctor", "--repo", str(target), "--skip-probe"])
    out = capsys.readouterr().out
    assert "routing:" in out
    assert "plan=qwen" in out
    assert "author=qwen" in out


@requires_git
def test_curate_unknown_backend_override_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`agora curate --backend NAME` with an undefined NAME exits non-zero with a clear message
    (the override escape hatch is fail-loud), not a traceback."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--domain", "ai-tech"]) == 0
    capsys.readouterr()

    rc = main(["curate", "--repo", str(target), "--force", "--backend", "nonesuch"])
    assert rc == 1
    assert "unknown backend" in capsys.readouterr().err


@requires_git
def test_curate_invalid_routing_block_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed ADR-0015 `routing:` block (an undefined target) makes `agora curate` exit
    non-zero with a clean 'invalid adapters.yaml' message, not an uncaught ValueError."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--domain", "ai-tech"]) == 0
    capsys.readouterr()
    adapters = target / "adapters.yaml"
    adapters.write_text(
        adapters.read_text(encoding="utf-8") + "routing:\n  plan: ghost\n", encoding="utf-8"
    )

    rc = main(["curate", "--repo", str(target), "--force"])
    assert rc == 1
    assert "invalid adapters.yaml" in capsys.readouterr().err


@requires_git
def test_doctor_tolerates_a_malformed_adapters_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], probe_env: None
) -> None:
    """`agora doctor` never crashes on a malformed adapters.yaml — it notes it on the routing line
    and still reports a status.

    (``probe_env`` since #96: the verdict assertion below is about the CONFIG-error path, so the
    other verdict contributor — the per-platform sandbox self-test — is pinned green.)"""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--domain", "ai-tech"]) == 0
    capsys.readouterr()
    (target / "adapters.yaml").write_text('a: "unterminated', encoding="utf-8")

    rc = main(["doctor", "--repo", str(target)])
    out = capsys.readouterr().out
    assert "routing:" in out
    assert "unreadable" in out
    assert "status:" in out
    # #96: the brain probe shares that ONE failed parse — it never re-reads the file and never
    # probes. It DOES fail the verdict: `adapters.yaml` is the only definition of every brain, so a
    # malformed one makes `_build_backend` return None and `agora curate` exit 1 (locked below).
    # Reporting `healthy` for a repo that cannot curate is issue #96's opening complaint.
    assert "brains: NOT CONFIGURED (adapters.yaml unreadable — see the routing line above)" in out
    assert "status: unhealthy" in out
    assert rc == 1
    # The operator gets the same actionable next step as any other brain failure (AMENDMENT A1).
    assert "fix (no download" in out

    # The verdict is not a guess: curation really is structurally impossible on this repo.
    capsys.readouterr()
    assert main(["curate", "--repo", str(target), "--force"]) == 1


@requires_git
def test_curate_with_stub_backend_publishes_and_query_reflects_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`curate --force` with a STUB backend PUBLISHES a theme; query then reflects it.

    End-to-end Phase-2 wiring: `repo init` -> a `kb_remember`-shaped capture -> an adapters.yaml
    stub brain -> `agora curate --force` runs curator.worker, which CREATE_THEMEs + authors prose +
    commits + CAS. After publish the owner working copy is fast-forwarded (ADR-0008
    read-after-publish), so the on-disk theme exists and `core.Wiki.query` resolves it.
    """
    target = tmp_path / "kb"
    assert (
        main(
            [
                "repo",
                "init",
                str(target),
                "--name",
                "personal",
                "--domain",
                "ai-tech",
                "--tag",
                "curator",
                "--tag",
                "concurrency",
            ]
        )
        == 0
    )
    capsys.readouterr()
    _write_stub_adapters(target)

    layout = RepoLayout(target)
    # Point the per-repo config's default brain at the stub (init wrote the OSS default 'qwen').
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("backend: qwen", "backend: stub"),
        encoding="utf-8",
    )
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )

    rc = main(["curate", "--repo", str(target), "--force"])
    assert rc == 0
    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert "status: published" in out
    assert "CREATE_THEME=1" in out

    # The published theme is on disk (read-after-publish sync) and the deterministic query finds it.
    theme = layout.wiki_dir / "concepts" / "curator-concurrency.md"
    assert theme.is_file()
    assert "The single curator holds a per-repo flock." in theme.read_text(encoding="utf-8")
    result = Wiki(layout).query("curator concurrency")
    assert result.status == "ok"
    assert any(h.path == "wiki/concepts/curator-concurrency.md" for h in result.hits)
    # The inbox is drained.
    assert Inbox(layout).depth() == 0
    # PASS 2 authored every region, so the run reports zero pending prose and warns about nothing.
    # (`err` is read from the SAME readouterr() call as `out` above — a second call would return an
    # already-drained buffer and the assertion could never fail.)
    assert "prose_pending=0" in out
    assert "warning:" not in err


# A brain whose PASS 1 plans normally but whose PASS 2 dies without writing a byte — the shape of a
# backend the sandbox could not start (#115: `bwrap: execvp …: No such file or directory`).
_SILENT_PASS2_BRAIN = _STUB_BRAIN.split("for note in")[0] + (
    'sys.stderr.write("simulated launch failure\\n")\nsys.exit(1)\n'
)


@requires_git
def test_curate_warns_loudly_when_pass2_authors_no_prose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A published-but-prose-less run must say so on STDERR — the #115 anti-silence guarantee.

    The run still publishes (structure is APPLY-owned and valid) and ``status: published`` is still
    correct, so the ONLY thing between the operator and a KB of empty bodies is this warning plus
    the ``prose_pending`` count. On Linux this failed for every region of every run and printed
    nothing at all.
    """
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    (target / "stub_brain.py").write_text(_SILENT_PASS2_BRAIN, encoding="utf-8")
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )
    capsys.readouterr()

    assert main(["curate", "--repo", str(target), "--force"]) == 0

    captured = capsys.readouterr()
    assert "status: published" in captured.out  # unchanged: the publish itself is correct
    assert "prose_pending=1" in captured.out
    assert "warning: PROSE PENDING" in captured.err
    # The backend's own stderr reaches the operator, so the cause is actionable, not a mystery.
    assert "simulated launch failure" in captured.err
    theme = layout.wiki_dir / "concepts" / "curator-concurrency.md"
    assert "_summary pending_" in theme.read_text(encoding="utf-8")


# A brain that cannot run at all — the shape of an absent/dead Ollama daemon (#96 criterion 6).
# A non-zero PASS 1 makes SubprocessBackend.plan raise BackendUnavailableError → worker._fail with
# a `PLAN-BACKEND:` reason.
_DEAD_BRAIN = 'import sys\nsys.stderr.write("ollama: connection refused\\n")\nsys.exit(1)\n'


def _dead_brain_repo(target: Path) -> RepoLayout:
    """A stub repo whose brain dies on PASS 1, with one event waiting — the first-run failure."""
    layout = _init_stub_repo(target)
    (target / "stub_brain.py").write_text(_DEAD_BRAIN, encoding="utf-8")
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )
    return layout


@requires_git
def test_curate_failed_run_prints_error_record_path_and_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#96 crit 6: a failed run points at its record and shows the head of it — on STDOUT.

    Before this, `agora curate` printed `status: failed` and stopped: the verdict was there but
    nothing led from it to the cause, and the lossless record under `_kb/failed/` was
    undiscoverable without knowing the layout. The two lines are fixed-arity `key: value` pairs in
    the same grammar as the `status:`/`counts:` lines they follow, so the machine-readable stdout
    summary is EXTENDED, not broken.
    """
    target = tmp_path / "kb"
    _dead_brain_repo(target)
    capsys.readouterr()

    assert main(["curate", "--repo", str(target), "--force"]) == 0

    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert "status: failed" in out
    records = [ln for ln in out.splitlines() if ln.startswith("failed_record: ")]
    checks = [ln for ln in out.splitlines() if ln.startswith("failed_checks: ")]
    assert len(records) == 1  # exactly one, deterministic — not one per failed check
    assert len(checks) == 1

    printed = records[0].removeprefix("failed_record: ")
    # Repo-RELATIVE POSIX (invariant 5: no host layout inside a repo-scoped string), in exactly the
    # `_kb/failed/<date>/<run-id>/error.json` shape criterion 6 names.
    parts = printed.split("/")
    assert parts[0] == "_kb"
    assert parts[1] == "failed"
    assert len(parts) == 5
    assert parts[-1] == "error.json"
    record = target / printed
    assert record.is_file()
    assert record.name == "error.json"
    assert json.loads(record.read_text(encoding="utf-8"))["failed_checks"]
    # ...and the printed head genuinely names the cause the operator has to act on.
    assert "PLAN-BACKEND" in checks[0]
    # STREAM DISCIPLINE: criterion 6 says stdout, and the cause is not duplicated onto stderr.
    assert "failed_record:" not in err


@requires_git
def test_curate_published_run_prints_no_failure_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run that PUBLISHED emits zero failure lines — the stdout arity of the happy path is
    byte-identical to pre-#96, so a machine consumer of `agora curate` cannot break."""
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )
    capsys.readouterr()

    assert main(["curate", "--repo", str(target), "--force"]) == 0

    out = capsys.readouterr().out
    assert "status: published" in out
    assert "failed_record:" not in out
    assert "failed_checks:" not in out
    assert "failed_requeue:" not in out  # #99: the hint rides the same gate, so it stays silent too


@requires_git
def test_curate_dead_brain_stdout_carries_no_warning_prose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The two channels stay separated: the FATAL cause is on stdout, and the free-form #115
    ``warning:`` prose never leaks into the machine-readable summary (nor is it duplicated —
    a PLAN failure that recorded its state cleanly produces no warning at all)."""
    target = tmp_path / "kb"
    _dead_brain_repo(target)
    capsys.readouterr()

    assert main(["curate", "--repo", str(target), "--force"]) == 0

    captured = capsys.readouterr()
    assert "warning:" not in captured.out
    assert "warning:" not in captured.err


@requires_git
def test_status_surfaces_a_non_terminal_failure_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#96 crit 7, driven through `agora status` as the criterion words it — the core gate.

    A failure INSIDE the retry budget is the blind spot: the events go back to ``inbox/`` (depth
    unchanged), ``last_run`` stays ``never`` because nothing PUBLISHED, ``counters.failed`` is not
    bumped, and nothing lands under ``_kb/failed/`` as a terminally-failed event. Before #96 the
    whole report was therefore indistinguishable from a repo that had simply never been curated.
    ``last_attempt`` + ``last_failure`` are the only signals that separate the two.

    (Lives next to the dead-brain curate tests because it is a curate → status end-to-end, not a
    rendering test: the state it reads was written by a REAL failed run, never planted.)
    """
    target = tmp_path / "kb"
    _dead_brain_repo(target)
    assert main(["curate", "--repo", str(target), "--force"]) == 0
    capsys.readouterr()

    assert main(["status", "--repo", str(target)]) == 0

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "last_run: never" in lines  # nothing published, and `last_run` must NOT lie about that
    assert "last_attempt: never" not in out  # ...yet the curator DID attempt a consolidation
    assert any(ln.startswith("last_attempt: 20") for ln in lines)
    assert any(ln.startswith("last_failure: UNRESOLVED ") for ln in lines)
    # Non-terminal: the event returned to the inbox, so the terminal backlog is still empty — which
    # is exactly why `failed_events` alone could never have surfaced this run.
    assert "failed_events: 0" in lines
    assert "inbox depth: 1" in lines
    assert "counters: ingested=0 merged=0 dropped=0 failed=0" in lines
    # The rendered pointer resolves to the real, lossless record.
    record = next(ln for ln in lines if ln.startswith("last_failure: ")).split("record=", 1)[1]
    assert (target / record.split(" first=", 1)[0]).is_file()


# --- requeue: the _kb/failed/ → _kb/inbox/ back-edge (issue #99) ---------------------------------
# What is locked here is the FACE: the byte-exact report block, the exit-code table, the stderr
# warnings and the discoverability hints. The engine's own invariants (lock span, rename-only,
# deletes-nothing, the drain rule) live in tests/curator/test_requeue.py.
_REQUEUE_RUN = "2026-06-13T03-00-00.000Z--04e370"


def _terminal_event(
    layout: RepoLayout,
    *,
    second: int,
    writer: str = "dochan",
    run_id: str = _REQUEUE_RUN,
    event_key: str | None = None,
) -> str:
    """Put ONE terminal-failure event where ``worker._fail`` leaves it; return its event id.

    Built by ``Inbox.write`` and then MOVED, exactly as the curator produces one, so the bytes the
    face reports on are a real event's bytes. ``lock_file.touch()`` reproduces the fact that a repo
    which has EVER had a terminal failure has necessarily taken ``curator_lock`` at least once —
    without it the "dry-run creates nothing" assertions would be about the documented R4 residual
    rather than about requeue.
    """
    receipt = Inbox(layout).write(
        text=f"A terminal capture at second {second}.",
        writer=writer,
        source="claude-code",
        event_key=event_key,
        now=datetime(2026, 6, 13, 2, 40, second, tzinfo=UTC),
    )
    dest_dir = layout.failed_dir / run_id[:10] / run_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.replace(layout.inbox_item_path(writer, receipt.id), dest_dir / f"{receipt.id}.md")
    layout.lock_file.touch()
    return receipt.id


def _error_record(layout: RepoLayout, *, event_ids: list[str], run_id: str = _REQUEUE_RUN) -> Path:
    """Write the ``error.json`` retry record ``worker._fail`` writes beside a terminal event."""
    dest_dir = layout.failed_dir / run_id[:10] / run_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / "error.json"
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "base_commit": "0" * 40,
                "event_ids": event_ids,
                "phase": "claimed",
                "failed_checks": ["TAXONOMY: unknown domain 'not-a-real-domain'"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _deliver(layout: RepoLayout, *, writer: str, event_key: str, event_id: str) -> None:
    """Record ``<writer>:<event_key>`` in ``state.event_keys`` — the tier-1 "already delivered"."""
    store = StateStore(layout)
    state = store.load()
    state.record_event_key(writer, event_key, event_id)
    store.save(state)


def _undry(lines: list[str]) -> list[str]:
    """Fold a dry-run report into its executed form — the ONLY difference a correct preview may
    have from the result, so normalizing it away turns crit 5 into a plain list equality."""
    return [
        line.replace("requeue [dry-run]:", "requeue:")
        .replace("reset_attempts [dry-run]:", "reset_attempts:")
        .replace("would requeue", "requeued")
        .replace("would skip", "skipped")
        for line in lines
    ]


def test_requeue_requires_a_selector(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """(#99 crit 9) No bare mass move. Nothing else in this CLI moves files en masse on a bare
    invocation, and `agora curate` hands the operator the exact `--run <id>` command, so the
    all-events move stays a deliberate act rather than the default."""
    with pytest.raises(SystemExit) as exc:
        main(["requeue", "--repo", str(tmp_path)])

    assert exc.value.code == 2
    assert "one of the arguments --run --event --all is required" in capsys.readouterr().err


def test_requeue_rejects_two_selectors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two selectors is an argparse error, not a silent precedence rule the operator must learn.

    Only the STABLE fragment is pinned: argparse names the LATER option, so the full sentence
    depends on flag order and would make this test a spelling lock rather than a behaviour lock.
    """
    with pytest.raises(SystemExit) as exc:
        main(["requeue", "--repo", str(tmp_path), "--run", "r1", "--all"])

    assert exc.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_requeue_all_on_an_empty_spool_is_a_clean_zero_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(#99 crit 5/9) ``--all`` asserts nothing exists, so an empty spool is SUCCESS — cron-safe.

    The whole zero block is locked because its point is a CONSTANT, greppable shape: a script
    reading `requeued:`/`skipped:`/`failed_events:` must not have to special-case "nothing to do".
    And the pristine repo stays pristine: the single permitted pre-lock filesystem access is the
    pure ``failed_event_count`` read, so ``curator_lock`` never runs and never creates ``_kb/``
    plus its 0-byte lock file (which would make criterion 5 literally false).
    """
    layout = RepoLayout(tmp_path)

    assert main(["requeue", "--repo", str(tmp_path), "--all"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        f"repo: {layout.root}",
        "requeue: selector=all matched=0",
        "requeued: 0",
        "skipped: 0",
        "failed_events: 0",
    ]
    assert not layout.kb_dir.exists()


def test_requeue_unknown_run_id_is_a_clear_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A NAMED selector asserts a specific thing exists, so a miss is rc 1 — unlike ``--all``."""
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)

    assert main(["requeue", "--repo", str(tmp_path), "--run", "nope"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "agora requeue: no failed run 'nope' under _kb/failed/"
    assert failed_event_count(layout) == 1  # ...and nothing was moved on the way to the error


def test_requeue_unknown_event_id_is_a_clear_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ``--event`` twin of the above — the operator pasted an id that is not in the spool."""
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)

    assert main(["requeue", "--repo", str(tmp_path), "--event", "not-an-id"]) == 1

    assert (
        capsys.readouterr().err.strip()
        == "agora requeue: no failed event 'not-an-id' under _kb/failed/"
    )


def test_requeue_prints_the_locked_report_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(#99 crit 3/6/7) The whole report, byte-for-byte, with all three verdicts in one run.

    Every element of the §4.3 grammar is here at once because they only mean something together:
    ``repo:`` first (the `agora status` house shape), one two-space-indented line per event LABELLED
    BY ITS FRONTMATTER ID (what the operator pastes back into ``--event``), repo-relative POSIX
    destinations (host-free, so the line can be pasted into an issue), the two summary lines that
    print even at 0, the per-reason tally with slugs sorted alphabetically, and the predicted
    ``failed_events:``. A decline is rc 0: the command DID its job and correctly refused.
    """
    layout = RepoLayout(tmp_path)
    movable = _terminal_event(layout, second=10)
    delivered = _terminal_event(layout, second=11, event_key="k2")
    occupied = _terminal_event(layout, second=12, writer="web")
    _deliver(layout, writer="dochan", event_key="k2", event_id=delivered)
    # An idempotent duplicate already holds the inbox slot — the one thing --force must not undo.
    taken = layout.inbox_item_path("web", occupied)
    taken.parent.mkdir(parents=True, exist_ok=True)
    taken.write_text("a different, immutable event\n", encoding="utf-8")

    assert main(["requeue", "--repo", str(tmp_path), "--all"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        f"repo: {layout.root}",
        "requeue: selector=all matched=3",
        f"  {movable}: requeued -> _kb/inbox/dochan/{movable}.md",
        f"  {delivered}: skipped (already-delivered: dochan:k2 is already in state.event_keys — "
        "the claim would drop it; use --force to move it anyway)",
        f"  {occupied}: skipped (destination-exists: _kb/inbox/web/{occupied}.md is already "
        "present — not overwritten)",
        "requeued: 1",
        "skipped: 2 (already-delivered=1 destination-exists=1)",
        "failed_events: 2",
        "note: fix the cause before curating again — 'agora status' shows the last failure, "
        "'agora doctor' checks the brain",
    ]
    assert taken.read_text(encoding="utf-8") == "a different, immutable event\n"  # never clobbered


def test_requeue_dry_run_report_predicts_the_real_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(#99 crit 5) The preview IS the result: one resolver, two callers, one list.

    Compared after folding ``would requeue``/``would skip`` back to their executed spelling — the
    only difference a correct dry run may have. ``failed_events:`` is compared UNFOLDED on purpose:
    it is the PREDICTED post-count in both modes (never the pre-count), which is the number the
    preview is actually promising to produce.
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)
    delivered = _terminal_event(layout, second=11, event_key="k2")
    _deliver(layout, writer="dochan", event_key="k2", event_id=delivered)

    assert main(["requeue", "--repo", str(tmp_path), "--all", "--dry-run"]) == 0
    preview = capsys.readouterr().out.splitlines()
    assert main(["requeue", "--repo", str(tmp_path), "--all"]) == 0
    real = capsys.readouterr().out.splitlines()

    assert _undry(preview) == real
    assert "failed_events: 1" in preview  # the PREDICTED post-count, not the pre-count of 2


def test_requeue_failed_events_line_matches_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(#99 crit 7) The backlog drops by exactly the moved count, and the two faces AGREE.

    String equality against `agora status`'s own line, not just numeric equality: the criterion is
    that an operator can run either command and read the same number off the same key.
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)
    _terminal_event(layout, second=11)
    _terminal_event(layout, second=12, run_id="2026-06-13T04-00-00.000Z--b17c91")

    assert main(["requeue", "--repo", str(tmp_path), "--run", _REQUEUE_RUN]) == 0
    requeued_line = next(
        ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("failed_events: ")
    )
    assert main(["status", "--repo", str(tmp_path)]) == 0
    status_line = next(
        ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("failed_events: ")
    )

    assert requeued_line == status_line == "failed_events: 1"
    assert failed_event_count(layout) == 1


def test_requeue_under_a_held_lock_is_a_clean_non_zero_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(#99 crit 4) A curate in flight ⇒ ONE stderr line, rc 1, no traceback, nothing moved.

    ``curator_lock`` is non-blocking (ADR-0008 step 1), so contention surfaces as ``LockHeld``.
    The face must translate it into a sentence an operator can act on — "try again when it
    finishes" — rather than a stack trace that reads like a bug in requeue. ONE line and no others:
    the repo below also has an UNRESOLVED failure, whose ``--all`` preflight warning runs as a hook
    INSIDE the lock span, so contention cannot leak a second sentence in front of the refusal.
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)
    _plant_failure(layout, run_id="r1")
    capsys.readouterr()

    with curator_lock(layout):
        rc = main(["requeue", "--repo", str(tmp_path), "--all"])

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err.strip() == (
        "agora requeue: a curator run is in progress (_kb/curator.lock is held); "
        "nothing was changed — try again when it finishes"
    )
    assert "Traceback" not in captured.err
    assert failed_event_count(layout) == 1


def test_requeue_force_moves_an_already_delivered_event_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(#99 crit 6, the ``--force`` half) The flag moves it AND names the consequence.

    A forced event keeps its own verdict word through execution, so the report says WHY a
    known-doomed event moved instead of hiding it behind a plain ``requeued`` — and the stderr line
    states the outcome plainly: the claim will drop it again. Pinning the consequence rather than
    suppressing it is what makes the flag safe to offer.
    """
    layout = RepoLayout(tmp_path)
    delivered = _terminal_event(layout, second=10, event_key="k1")
    _deliver(layout, writer="dochan", event_key="k1", event_id=delivered)

    assert main(["requeue", "--repo", str(tmp_path), "--all", "--force"]) == 0

    captured = capsys.readouterr()
    assert (
        f"  {delivered}: requeued (already-delivered; forced) -> _kb/inbox/dochan/{delivered}.md"
    ) in captured.out
    assert "requeued: 1" in captured.out
    assert "skipped: 0" in captured.out
    assert captured.err.strip() == (
        "warning: 1 forced event(s) carry an event_key already in state.event_keys — "
        "the claim will drop them again"
    )
    assert layout.inbox_item_path("dochan", delivered).is_file()


def test_requeue_refuses_an_unreadable_state_json_and_force_proceeds_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A safety check that evaporates on a corrupt file is not a safety check (#99 crit 6).

    Both halves in one test because they are one decision: WITHOUT ``--force`` the unreadable
    state is a clean rc 1 with nothing moved (the pre-check cannot run, so requeue does not
    pretend it did); WITH it the operator has explicitly accepted the zombie risk, so the move
    proceeds — but the skipped pre-check is said out loud on stderr, never assumed understood.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    layout.state_file.write_text("not json at all", encoding="utf-8")

    assert main(["requeue", "--repo", str(tmp_path), "--all"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("agora requeue: _kb/state.json is unreadable — ")
    assert "(use --force to requeue without the tier-1 pre-check)" in captured.err
    assert "Traceback" not in captured.err
    assert failed_event_count(layout) == 1

    assert main(["requeue", "--repo", str(tmp_path), "--all", "--force"]) == 0
    captured = capsys.readouterr()
    assert "the already-delivered pre-check was SKIPPED (--force)" in captured.err
    assert layout.inbox_item_path("dochan", event).is_file()


def test_requeue_reports_an_event_it_could_not_move(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file under ``_kb/failed/`` that is not a usable event is REPORTED and LEFT ALONE.

    ``_kb/failed/`` is operator-editable, so this is a real shape: something hand-copied in, or an
    event whose frontmatter was mangled. rc 1 because the command genuinely could not do its job
    for that item, while the movable ones in the same batch still move — a batch must never be
    all-or-nothing when the failure is per-file.
    """
    layout = RepoLayout(tmp_path)
    good = _terminal_event(layout, second=10)
    junk = layout.failed_dir / _REQUEUE_RUN[:10] / _REQUEUE_RUN / "0000-not-an-event.md"
    junk.write_text("no frontmatter here\n", encoding="utf-8")

    assert main(["requeue", "--repo", str(tmp_path), "--all"]) == 1

    captured = capsys.readouterr()
    assert "  0000-not-an-event: skipped (unreadable: " in captured.out
    assert f"  {good}: requeued -> " in captured.out
    assert "requeued: 1" in captured.out
    assert "skipped: 1 (unreadable=1)" in captured.out
    assert captured.err.strip() == (
        "warning: 1 event(s) under _kb/failed/ could not be requeued and were left in place"
    )
    assert junk.read_text(encoding="utf-8") == "no frontmatter here\n"


def test_requeue_all_warns_when_the_last_failure_is_unresolved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--all`` into a still-broken curator gets a warning; the narrow selectors stay silent.

    The warning exists because a mass requeue with the cause unfixed re-terminalises the whole
    batch on the next tick. It is deliberately NOT emitted for ``--run``/``--event``: those are the
    narrow, advertised forms (`agora curate` prints the exact `--run` command), and warning on the
    recommended path is how a warning becomes noise nobody reads.
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)
    _terminal_event(layout, second=11)
    _plant_failure(layout, run_id="r1")
    capsys.readouterr()

    assert main(["requeue", "--repo", str(tmp_path), "--all", "--dry-run"]) == 0
    assert capsys.readouterr().err.strip() == (
        "warning: the last curator failure is still UNRESOLVED (run=r1 "
        "record=_kb/failed/2026-06-13/r1/error.json) — run 'agora doctor' first; "
        "a requeued event goes terminal again on the next failing run"
    )

    assert main(["requeue", "--repo", str(tmp_path), "--run", _REQUEUE_RUN]) == 0
    assert "UNRESOLVED" not in capsys.readouterr().err


def test_requeue_all_is_not_blocked_by_an_unresolved_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The anti-guard lock: UNRESOLVED is a WARNING, never a refusal (#99 §4.3).

    ``failure_is_current`` is True on 100% of legitimate recoveries — ``last_run`` is the last
    successful PUBLISH and the canonical order is fix → requeue → curate — so a "refuse unless
    --force" gate would fire every single time, train reflexive ``--force``, and collide with
    criterion 6's use of the same flag. If a future change adds that gate, this fails.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    _plant_failure(layout, run_id="r1")
    capsys.readouterr()

    assert main(["requeue", "--repo", str(tmp_path), "--all"]) == 0

    assert layout.inbox_item_path("dochan", event).is_file()
    assert failed_event_count(layout) == 0
    assert "requeued: 1" in capsys.readouterr().out


def test_requeue_reset_attempts_block_is_identical_in_both_modes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(#99 crit 5 ∩ 8) Only the HEADER carries ``[dry-run]``; the indented lines are byte-equal.

    That is what makes "the archive list matches the result" a literal string equality rather than
    a hope: the drain rule computes ``remaining`` the same way in both modes (planned moves in
    dry-run, actual ones in a real run), so the ``archived:`` lines cannot drift.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    _error_record(layout, event_ids=[event])
    archived_line = (
        f"  archived: _kb/failed/2026-06-13/{_REQUEUE_RUN}/error.json "
        f"-> _kb/requeued/2026-06-13/{_REQUEUE_RUN}/error.json"
    )

    assert main(["requeue", "--repo", str(tmp_path), "--all", "--dry-run", "--reset-attempts"]) == 0
    preview = capsys.readouterr().out.splitlines()
    assert main(["requeue", "--repo", str(tmp_path), "--all", "--reset-attempts"]) == 0
    real = capsys.readouterr().out.splitlines()

    assert "reset_attempts [dry-run]: archived=1 kept=0" in preview
    assert "reset_attempts: archived=1 kept=0" in real
    assert archived_line in preview
    assert archived_line in real
    assert _undry(preview) == real
    assert (layout.requeued_dir / "2026-06-13" / _REQUEUE_RUN / "error.json").is_file()


def test_requeue_reset_attempts_that_reset_nothing_says_so_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A flag that moved events and reset NOTHING must say so — silence here is a trap.

    One record governs two events and only one is selected, so the drain rule correctly KEEPS it
    (criterion 9: no unselected event's budget may drop). The operator would otherwise walk into
    the next run believing the budget was restored, and watch the event go terminal immediately.
    """
    layout = RepoLayout(tmp_path)
    one = _terminal_event(layout, second=10)
    two = _terminal_event(layout, second=11)
    _error_record(layout, event_ids=[one, two])

    assert main(["requeue", "--repo", str(tmp_path), "--event", one, "--reset-attempts"]) == 0

    captured = capsys.readouterr()
    assert "reset_attempts: archived=0 kept=1" in captured.out
    assert (
        f"  kept: _kb/failed/2026-06-13/{_REQUEUE_RUN}/error.json "
        "(records an event that is still terminal)"
    ) in captured.out
    assert "warning: --reset-attempts reset nothing" in captured.err
    assert "use --run or --all to reset the whole set" in captured.err
    assert (layout.failed_dir / "2026-06-13" / _REQUEUE_RUN / "error.json").is_file()
    assert not layout.requeued_dir.exists()
    assert failed_event_count(layout) == 1  # `two` is untouched — the selector scoped the move


def test_requeue_typo_in_a_named_selector_is_an_error_even_with_reset_attempts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(#99 §4.4) A mistyped ``--run`` is rc 1 and drains NOTHING, whatever else is drainable.

    The two scopes differ — the selector picks EVENTS, the drain rule releases RECORDS — so a
    "not found" decided on anything but the selection would make the exit code of one operator
    mistake depend on unrelated repo state, and would silently reset a budget nobody asked about.
    The repo here has a genuinely drainable record (crash residue: its event is back in the inbox),
    which is exactly the state that used to turn this typo into a reported success.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    _error_record(layout, event_ids=[event])
    os.replace(
        layout.failed_dir / "2026-06-13" / _REQUEUE_RUN / f"{event}.md",
        layout.inbox_item_path("dochan", event),
    )
    capsys.readouterr()

    rc = main(["requeue", "--repo", str(tmp_path), "--run", "no-such-run", "--reset-attempts"])

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err.strip() == "agora requeue: no failed run 'no-such-run' under _kb/failed/"
    assert (layout.failed_dir / "2026-06-13" / _REQUEUE_RUN / "error.json").is_file()
    assert not layout.requeued_dir.exists()


def test_requeue_reset_attempts_with_no_records_at_all_says_that(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``archived=0`` has three causes and the warning must name the right one.

    Here there is no ``error.json`` anywhere, so "every record is shared with an event that is still
    terminal" would be flatly false and would send the operator to ``--all`` to be told the same
    thing again. The ``kept:`` lines the other wording tells them to read do not exist either.
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)
    capsys.readouterr()

    assert main(["requeue", "--repo", str(tmp_path), "--all", "--reset-attempts"]) == 0

    captured = capsys.readouterr()
    assert "reset_attempts: archived=0 kept=0" in captured.out
    assert captured.err.strip() == (
        "warning: --reset-attempts reset nothing — the requeued events have no retry records "
        "under _kb/failed/, so there was no spent budget to restore"
    )


def test_requeue_reports_a_retry_record_it_could_not_archive_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only disk means the budget was NOT reset — say so, and do not exit 0.

    `deploy/README.md` documents this command inside a scripted recovery, so rc 0 here would let a
    cron/launchd wrapper record success over a reset that never happened. The old loud-null wording
    made it worse than silence: it diagnosed a shared record and pointed at ``--all``, away from the
    real cause. The events themselves DID move, which is why the report still prints.
    """
    layout = RepoLayout(tmp_path)
    event = _terminal_event(layout, second=10)
    _error_record(layout, event_ids=[event])
    real_replace = os.replace

    def _refuse_the_record(src: object, dst: object) -> None:
        if str(src).endswith("error.json"):
            raise OSError(30, "Read-only file system")
        real_replace(src, dst)

    monkeypatch.setattr(requeue_mod.os, "replace", _refuse_the_record)
    capsys.readouterr()

    assert main(["requeue", "--repo", str(tmp_path), "--all", "--reset-attempts"]) == 1

    captured = capsys.readouterr()
    assert "requeued: 1" in captured.out
    assert "reset_attempts: archived=0 kept=1" in captured.out
    assert "(could not be archived: [Errno 30] Read-only file system)" in captured.out
    assert captured.err.strip() == (
        "warning: 1 retry record(s) could not be archived — the budget of the events they list "
        "was NOT reset (see the kept: lines for the reason)"
    )
    assert "reset nothing" not in captured.err


def test_requeue_renders_an_error_outcome_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(#99 §4.3/§4.4) The ``error`` row and the ``errors:`` summary line, on the real renderer.

    An error is deliberately NOT rendered as a skip: a skip is a correct refusal, an error is work
    that could not be done. Both spellings and the rc-1 row were reachable but unlocked, so a
    renderer refactor could have silently folded them into the ``skipped`` branch.
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)

    def _explode(layout: RepoLayout, event_path: Path) -> InboxReturn:  # noqa: ARG001
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(requeue_mod, "return_event_to_inbox", _explode)
    capsys.readouterr()

    assert main(["requeue", "--repo", str(tmp_path), "--all"]) == 1

    captured = capsys.readouterr()
    assert ": error ([Errno 28] No space left on device)" in captured.out
    assert "errors: 1" in captured.out
    assert "requeued: 0" in captured.out
    assert failed_event_count(layout) == 1


def test_requeue_dry_run_warnings_do_not_claim_completed_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every other line of the report is mode-aware; the stderr warnings must be too.

    A preview that says events "were left in place" or that forced ones "carry" a delivered key is
    reporting a past tense for work nobody has done yet — and it is the same operator who is about
    to decide whether to run it for real.
    """
    layout = RepoLayout(tmp_path)
    delivered = _terminal_event(layout, second=10, event_key="k1")
    _deliver(layout, writer="dochan", event_key="k1", event_id="older")
    junk = layout.failed_dir / "2026-06-13" / _REQUEUE_RUN / "0000-not-an-event.md"
    junk.write_text("no frontmatter here\n", encoding="utf-8")
    capsys.readouterr()

    assert main(["requeue", "--repo", str(tmp_path), "--all", "--force", "--dry-run"]) == 1

    err = capsys.readouterr().err
    assert "1 forced event(s) would carry an event_key already in state.event_keys" in err
    assert "1 event(s) under _kb/failed/ cannot be requeued and would be left in place" in err
    assert failed_event_count(layout) == 2  # the preview really previewed
    assert not layout.inbox_item_path("dochan", delivered).exists()


def test_doctor_offers_requeue_when_state_json_is_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corrupt ``state.json`` is the repo where requeue is MOST likely the next step.

    ``agora requeue --all --force`` is built precisely for it (``--force`` relaxes the tier-1
    pre-check's state load), and ``events`` is already known on this branch — withholding the
    backlog line here would leave the designated pull surface silent about the one remedy. Only the
    "fix the cause above" prefix is dropped: there is no ``failure_is_current`` to consult.
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)
    layout.state_file.write_text("{ not json", encoding="utf-8")

    main(["doctor", "--repo", str(tmp_path)])

    out = capsys.readouterr().out
    assert "  failures: events=1 state.json unreadable (" in out
    assert (
        "  requeue: 1 terminal event in _kb/failed/ — "
        "'agora requeue --all' returns the backlog to the inbox"
    ) in out
    assert "status:" in out  # the verdict is still reached


# --- requeue: discoverability (#99 crit 10) ------------------------------------------------------
@requires_git
def test_curate_terminal_failure_advertises_requeue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(#99 crit 10) A run that left events in ``_kb/failed/`` prints the way BACK — and it works.

    The hint is not asserted as a string and left there: the printed command is EXECUTED, and the
    backlog it names really empties. That is the criterion ("curate 실패 출력에서 이 커맨드를
    안내한다") tested as a round trip rather than as a spelling, so a rename of the selector or a
    change to the run-id ⇄ ``_kb/failed/<date>/<run-id>/`` mapping fails here.
    """
    target = tmp_path / "kb"
    layout = _dead_brain_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    doc = yaml.safe_load(repo_yaml.read_text(encoding="utf-8"))
    doc["curator"]["max_attempts"] = 1  # one failure is terminal — no waiting out the budget
    repo_yaml.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    capsys.readouterr()

    assert main(["curate", "--repo", str(target), "--force"]) == 0

    out = capsys.readouterr().out
    assert "failed=1" in out
    hints = [ln for ln in out.splitlines() if ln.startswith("failed_requeue: ")]
    assert len(hints) == 1
    run_id = hints[0].removeprefix("failed_requeue: agora requeue --run ")
    assert (layout.failed_dir / run_id[:10] / run_id).is_dir()
    assert failed_event_count(layout) == 1

    assert main(["requeue", "--repo", str(target), "--run", run_id]) == 0
    assert failed_event_count(layout) == 0


@requires_git
def test_curate_within_budget_failure_does_not_advertise_requeue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate is ``counts["failed"] > 0``, not "the run failed" — advertising here is WRONG.

    A within-budget failure returns its events to ``inbox/`` (``retried=1 failed=0``); there is
    nothing under ``_kb/failed/`` to requeue, so the hint would send an operator to a command that
    reports "no failed run" — worse than silence, because it implies the events were lost.
    """
    target = tmp_path / "kb"
    layout = _dead_brain_repo(target)  # default curator.max_attempts=3 ⇒ attempt 1 is a retry
    capsys.readouterr()

    assert main(["curate", "--repo", str(target), "--force"]) == 0

    out = capsys.readouterr().out
    assert "retried=1" in out
    assert "failed_record: " in out  # the CAUSE is still advertised — only the remedy is withheld
    assert "failed_requeue:" not in out
    assert failed_event_count(layout) == 0
    assert Inbox(layout).depth() == 1


def test_cas_conflict_failure_does_not_advertise_requeue(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CAS-conflict shape — ``record_path=None``, events back in ``inbox/`` — stays silent.

    Driven through the printer directly rather than through a manufactured concurrent publish: the
    unit under test IS the face's gate, and the report shape it must gate on is fully specified by
    ``worker._fail``'s CAS branch (no error record, ``counts={"retried": N}``, no ``failed`` key).
    """
    report = RunReport(
        run_id=_REQUEUE_RUN,
        status="failed",
        counts={"retried": 2},
        failure=RunFailure(
            run_id=_REQUEUE_RUN,
            phase="claimed",
            reasons=("CAS: the curated ref moved since base_commit",),
            record_path=None,
            cas_conflict=True,
        ),
    )

    cli_mod._print_run_diagnostics(report)

    out = capsys.readouterr().out
    assert "failed_record: -" in out
    assert "failed_checks: CAS: " in out
    assert "failed_requeue:" not in out


@requires_git
def test_watch_tick_stamps_the_requeue_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The daemon is where terminalisation actually happens, so the hint must ride the tick log.

    Stamped like the rest of the ``{stamp} <verb>:`` grammar — `journalctl -u agora-watch` stays
    ONE stream — and inherited for free, because both faces render through the single
    ``_print_run_diagnostics`` channel rather than each growing their own copy (#115's rule).
    """
    target = tmp_path / "kb"
    layout = _dead_brain_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    doc = yaml.safe_load(repo_yaml.read_text(encoding="utf-8"))
    doc["curator"]["max_attempts"] = 1
    doc["curator"]["triggers"]["threshold"] = 1
    repo_yaml.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    lines = capsys.readouterr().out.splitlines()
    ran = next(ln for ln in lines if " ran (" in ln)
    hints = [ln for ln in lines if " failed_requeue: " in ln]
    assert len(hints) == 1
    stamp = ran.split(" ", 1)[0]
    assert hints[0].startswith(f"{stamp} failed_requeue: agora requeue --run ")


def test_doctor_offers_requeue_only_when_the_backlog_is_non_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Doctor is the PULL surface for the backlog — the only one that also holds the CAUSE.

    All three states in one test because the wording IS the sequencing: no backlog ⇒ no line at
    all; a backlog with no current failure ⇒ a bare offer; a backlog with the failure still
    UNRESOLVED ⇒ "fix the cause above, then …", which is literally true because ``_doctor_failures``
    runs LAST among the ``_doctor_*`` helpers. Observability only — the verdict is untouched.
    """
    layout = RepoLayout(tmp_path)

    main(["doctor", "--repo", str(tmp_path)])
    assert "requeue:" not in capsys.readouterr().out

    _terminal_event(layout, second=10)
    main(["doctor", "--repo", str(tmp_path)])
    assert (
        "  requeue: 1 terminal event in _kb/failed/ — "
        "'agora requeue --all' returns the backlog to the inbox"
    ) in capsys.readouterr().out

    _terminal_event(layout, second=11)
    _plant_failure(layout, run_id="r1")
    main(["doctor", "--repo", str(tmp_path)])
    out = capsys.readouterr().out
    assert (
        "  requeue: 2 terminal events in _kb/failed/ — fix the cause above, then "
        "'agora requeue --all' returns the backlog to the inbox"
    ) in out
    assert "status:" in out  # the verdict is still reached


def test_doctor_requeue_count_uses_the_shared_helper(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """(#99 crit 7) "the same helper" is a claim value equality can never prove.

    Forcing ``failed_event_count`` to an impossible value is what makes it testable: a doctor line
    that grew its own ``rglob`` would keep every count assertion green while quietly diverging from
    `agora status` and MCP ``kb_status``.
    """
    monkeypatch.setattr(cli_mod, "failed_event_count", lambda layout: 99)

    main(["doctor", "--repo", str(tmp_path)])

    assert "  requeue: 99 terminal events in _kb/failed/ — " in capsys.readouterr().out


def test_status_does_not_advertise_requeue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The NEGATIVE lock (#99 §5.3): a later drive-by addition has to argue with a test.

    Every `agora status` line is ``key: <machine-readable value>`` and a remediation sentence has
    no value slot — but the real reason is ORDERING: status carries no cause information, so
    advertising requeue here would route the operator around `agora doctor`, inverting the exact
    sequencing the #99 risk section demands.
    """
    layout = RepoLayout(tmp_path)
    _terminal_event(layout, second=10)
    _plant_failure(layout, run_id="r1")

    assert main(["status", "--repo", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "failed_events: 1" in out
    assert "requeue" not in out


def test_status_and_doctor_follow_a_requeued_failure_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--reset-attempts`` moves the record, so the two renderers follow it (#99 §3.5).

    ``last_failure`` is never cleared by a later success and requeue is strictly rename-only (it
    must NOT clear a failure the operator has not fixed — the blind spot #96 closed), so without
    this the stored ``record=`` would point at nothing, indefinitely.

    The COMPAT half runs first and is the load-bearing one: with no twin on disk the byte-identical
    stored string is printed, which is why the two existing tests that lock a ``record=`` whose file
    does not exist keep passing unmodified.
    """
    layout = RepoLayout(tmp_path)
    _plant_failure(layout, run_id="r1")
    original = "_kb/failed/2026-06-13/r1/error.json"
    twin = "_kb/requeued/2026-06-13/r1/error.json"

    assert main(["status", "--repo", str(tmp_path)]) == 0
    assert f"record={original} first=" in capsys.readouterr().out
    main(["doctor", "--repo", str(tmp_path)])
    assert f"record={original}" in capsys.readouterr().out

    (tmp_path / twin).parent.mkdir(parents=True)
    (tmp_path / twin).write_text("{}\n", encoding="utf-8")

    assert main(["status", "--repo", str(tmp_path)]) == 0
    # No suffix, no annotation: `_kb/requeued/` is self-describing, and a space inside the value
    # would break the `record=… first=…` grammar scripts already split on.
    assert f"record={twin} first=" in capsys.readouterr().out
    main(["doctor", "--repo", str(tmp_path)])
    assert f"record={twin}" in capsys.readouterr().out


def test_record_pointer_survives_a_hostile_record_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``record_path`` is an unconstrained str in an operator-editable file — stat it defensively.

    ``Path.exists()`` RAISES ``ENAMETOOLONG`` (it is not in pathlib's ignored-errno set) and
    ``_fmt_last_failure`` is called OUTSIDE ``_cmd_status``'s try, so an unguarded stat would
    re-introduce exactly the traceback #96 removed. Driven twice: once end-to-end through a real
    over-long component, and once with the OSError forced, so the guard is proven even on a
    filesystem that would have tolerated the path.
    """
    layout = RepoLayout(tmp_path)
    hostile = "_kb/failed/2026-06-13/" + "x" * 400 + "/error.json"
    store = StateStore(layout)
    state = store.load()
    state.last_attempt = datetime(2026, 6, 13, 3, 0, 12, tzinfo=UTC)
    state.last_failure = LastFailure.from_run_failure(
        when=datetime(2026, 6, 13, 3, 0, 12, tzinfo=UTC),
        run_id="r1",
        phase="claimed",
        reasons=["boom"],
        record_path=hostile,
    )
    store.save(state)

    assert main(["status", "--repo", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert f"record={hostile} first=boom" in captured.out
    assert "Traceback" not in captured.err

    monkeypatch.setattr(
        Path, "exists", lambda self, **kw: (_ for _ in ()).throw(OSError(63, "File name too long"))
    )
    assert cli_mod._record_pointer(layout, "_kb/failed/2026-06-13/r1/error.json") == (
        "_kb/failed/2026-06-13/r1/error.json"
    )


# --- doctor -------------------------------------------------------------------------------------
def test_doctor_prints_report_and_returns_health_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["doctor", "--repo", str(tmp_path)])
    out = capsys.readouterr().out
    assert "agora doctor" in out
    assert "python:" in out
    assert "status:" in out
    # pydantic is a hard core dep and importable in the test env, so it must report ok.
    assert "dep pydantic: ok" in out
    # Health is binary: 0 (healthy) or 1 (unhealthy); both are valid outcomes for this report.
    assert rc in (0, 1)


def test_doctor_header_is_a_paste_ready_bug_report_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#101 criterion 4: doctor's FIRST line names the build and the interpreter.

    A bug report that opens with "which version?" costs a round trip, and before #101 doctor
    reported python, git, deps, sandbox, routing — everything except agora's own version. The line
    is asserted whole (not just "the version appears somewhere") because its value is that it can
    be copied in isolation; and the ``agora doctor`` prefix is asserted to still LEAD it, since the
    older assertions across this file — and any operator's grep — key on that token.
    """
    assert main(["doctor", "--repo", str(tmp_path)]) in (0, 1)
    first = capsys.readouterr().out.splitlines()[0]

    v = sys.version_info
    assert first == f"agora doctor (agora {__version__}, python {v.major}.{v.minor}.{v.micro})"
    assert first.startswith("agora doctor")


@requires_git
def test_doctor_reports_initialized_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "kb"
    Repo.resolve(target).init()
    rc = main(["doctor", "--repo", str(target)])
    assert rc in (0, 1)
    assert "initialized" in capsys.readouterr().out


@requires_git
def test_doctor_notes_line_counts_what_the_curator_does_not_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#152: the operator surface for a note the curator reads but will never curate.

    Before #152 such a note was worse than invisible — it failed every run. Now it is quietly
    excluded, which is only honest if somebody says so; this is the line that does. It never moves
    the verdict: writing in `wiki/` is supported, not ill health.

    (No ``--skip-probe``: a bare repo has no adapters.yaml, so no brain probe and no network call.)
    """
    target = tmp_path / "kb"
    Repo.resolve(target).init()

    main(["doctor", "--repo", str(target)])
    # The seed index.md carries the engine's `timestamp:` stamp, so a fresh repo owns everything.
    assert "  notes: 1 total, none out of schema" in capsys.readouterr().out

    hand_written = target / "wiki" / "concepts" / "human-note.md"
    hand_written.parent.mkdir(parents=True, exist_ok=True)
    hand_written.write_text("# Just a note\n\nStraight from Obsidian.\n", encoding="utf-8")

    rc = main(["doctor", "--repo", str(target)])
    out = capsys.readouterr().out
    assert "  notes: 2 total, 1 out of schema (read + indexed, never curated)" in out
    assert rc in (0, 1)  # observability only — the count never decides the verdict


def test_doctor_failures_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """#96 (doctor half): the failure backlog + last attempt/failure, print-only and crash-free.

    All three renderings in one test because the third is the point: doctor is what an operator
    runs when things are broken, so a corrupt ``state.json`` — the single most likely tick failure
    — must degrade to a REPORT and still let the run reach its `status:` verdict. The line never
    touches the verdict either way (an unreadable derived file is not ill health; `agora status`
    and `agora watch` report it loudly).

    (No ``--skip-probe``: a bare directory has no adapters.yaml, so the brain probe never runs and
    this test makes no network call.)
    """
    layout = RepoLayout(tmp_path)

    main(["doctor", "--repo", str(tmp_path)])
    assert "  failures: events=0 last_attempt=never last_failure=none" in capsys.readouterr().out

    run_dir = layout.failed_dir / "2026-06-13" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "a.md").write_text("event a", encoding="utf-8")
    (run_dir / "b.md").write_text("event b", encoding="utf-8")
    _plant_failure(layout, run_id="r1")

    main(["doctor", "--repo", str(tmp_path)])
    assert (
        "  failures: events=2 last_attempt=2026-06-13T03:00:12Z last_failure=UNRESOLVED run=r1 "
        "record=_kb/failed/2026-06-13/r1/error.json"
    ) in capsys.readouterr().out

    layout.state_file.write_text("not json at all", encoding="utf-8")

    main(["doctor", "--repo", str(tmp_path)])
    captured = capsys.readouterr()
    assert "  failures: events=2 state.json unreadable (" in captured.out
    assert "status:" in captured.out  # the verdict is still reached — that is the whole point
    assert "Traceback" not in captured.err


# --- doctor: the brain-availability probe (#96 criteria 1-5) -------------------------------------
# `repo init` wires the OSS default brain, so this is the shape doctor probes out of the box.
_OLLAMA_ADAPTERS = (
    "backends:\n  qwen: { argv: [agora-ollama-brain], network: loopback }\ndefault_backend: qwen\n"
)


def _probe_repo(target: Path, adapters: str, *, backend: str = "qwen") -> Path:
    """An initialized repo whose adapters.yaml is exactly ``adapters`` — the probe's only input.

    ``backend`` rewrites repo.yaml's ``curator.backend``, which is the ADR-0015 precedence a real
    run uses (``routing[act]`` → repo default → registry default): leaving it at the init-time
    ``qwen`` while the registry defines something else would resolve to an UNKNOWN backend, which
    is a different test.
    """
    assert main(["repo", "init", str(target), "--domain", "ai-tech"]) == 0
    (target / "adapters.yaml").write_text(adapters, encoding="utf-8")
    repo_yaml = RepoLayout(target).kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("backend: qwen", f"backend: {backend}"),
        encoding="utf-8",
    )
    return target


def _dead_daemon(message: str = "boom"):  # type: ignore[no-untyped-def]
    """A ``list_ollama_models`` stand-in for a daemon that cannot be reached.

    Keyword-ONLY ``timeout`` on every fake in this section is deliberate: a positional call from
    the probe would raise ``TypeError`` here, so the #96 criterion-5 call shape is locked by
    construction rather than by one dedicated assertion.
    """

    def fake(host: str, *, timeout: float) -> list[str]:
        raise ollama_brain.BrainError(message)

    return fake


def _installed_models(*names: str):  # type: ignore[no-untyped-def]
    """A ``list_ollama_models`` stand-in for a reachable daemon carrying ``names``."""

    def fake(host: str, *, timeout: float) -> list[str]:
        return list(names)

    return fake


def _live_ping(calls: list[str] | None = None):  # type: ignore[no-untyped-def]
    """A ``ping_ollama`` stand-in for a daemon that answers (#129). Same keyword-only shape."""

    def fake(host: str, *, timeout: float) -> None:
        if calls is not None:
            calls.append(host)

    return fake


def _dead_ping(message: str = "refused"):  # type: ignore[no-untyped-def]
    """A ``ping_ollama`` stand-in for a daemon that is DOWN (#129)."""

    def fake(host: str, *, timeout: float) -> None:
        raise ollama_brain.BrainError(message)

    return fake


def _which_only(installed: set[str]):  # type: ignore[no-untyped-def]
    """A ``shutil.which`` that pins WHICH known CLI agents resolve; everything else stays real.

    Whether ``claude`` / ``codex`` / ``gemini`` are on PATH is a property of the DEVELOPER's host,
    and it decides which remediation branch (AMENDMENT A1) doctor prints — so the tests that assert
    those bytes must own the answer. ``git`` and the interpreter still resolve for real.
    """
    real = shutil.which

    def which(cmd, mode=os.F_OK | os.X_OK, path=None):  # type: ignore[no-untyped-def]
        if cmd in {name for name, _ in cli_agent_brain.KNOWN_CLI_AGENTS}:
            return f"/usr/local/bin/{cmd}" if cmd in installed else None
        return real(cmd, mode, path)

    return which


@pytest.fixture
def probe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make doctor's verdict a pure function of the BRAIN probe, on any host.

    Four host dependencies are pinned:

    * the ADR-0013 sandbox leg is forced green (it is the other verdict contributor and it
      legitimately differs per platform);
    * ``$AGORA_OLLAMA_MODEL`` is unset (a dev machine with a pinned model would otherwise change
      the ``would use`` rendering);
    * ``$AGORA_OLLAMA_HOST`` is unset — ``parse_shim_args`` builds the shim's parser at probe time,
      so the ``--host`` default is read from the environment and every ``http://localhost:11434``
      assertion below would fail on a developer running Ollama elsewhere;
    * ``agora-ollama-brain`` resolves to a real executable, because the probe now asks the argv[0]
      question for the shim too (#96 crit 4) and whether the console script is on THIS host's PATH
      is not what these tests are about. Everything else still resolves for real.

    ``ping_ollama`` is stubbed LIVE for the same reason ``list_ollama_models`` always was: since
    #129 the pinned-model path contacts the daemon, so an unstubbed test would reach the real
    network and pass or fail on whether the developer happens to be running Ollama.
    """
    monkeypatch.setattr(cli_mod, "_doctor_sandbox", lambda *a, **k: True)
    monkeypatch.delenv(ollama_brain.MODEL_ENV, raising=False)
    monkeypatch.delenv("AGORA_OLLAMA_HOST", raising=False)
    monkeypatch.setattr(shutil, "which", _shim_which())
    monkeypatch.setattr(ollama_brain, "ping_ollama", _live_ping())


def _shim_which():  # type: ignore[no-untyped-def]
    """A ``shutil.which`` that resolves the Ollama shim to a real executable; the rest is real."""
    real = shutil.which

    def which(cmd, mode=os.F_OK | os.X_OK, path=None):  # type: ignore[no-untyped-def]
        if cmd == ollama_brain.CONSOLE_SCRIPT:
            return sys.executable
        return real(cmd, mode, path)

    return which


@requires_git
def test_doctor_unreachable_ollama_is_unhealthy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """#96 crit 1: an unreachable Ollama daemon makes `agora doctor` RED.

    The paired control in the same test (a reachable daemon → healthy) is what proves the probe IS
    the verdict rather than decoration: without it, a doctor that always returned 1 would pass.
    """
    target = _probe_repo(tmp_path / "kb", _OLLAMA_ADAPTERS)
    monkeypatch.setattr(ollama_brain, "list_ollama_models", _dead_daemon())
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert "  brain qwen: ollama http://localhost:11434 UNREACHABLE (boom)" in out
    assert "status: unhealthy" in out
    assert rc == 1

    monkeypatch.setattr(ollama_brain, "list_ollama_models", _installed_models("qwen3.6:35b-a3b"))
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert "  brain qwen: ollama http://localhost:11434 reachable, 1 models, would use " in out
    assert "status: healthy" in out
    assert rc == 0


def test_doctor_brains_returns_false_on_unreachable(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit under the crit-1 CLI test: the probe RESULT is what returns False (no repo/rc)."""
    from agora_kb.curator.backends import BackendRegistry

    monkeypatch.delenv(ollama_brain.MODEL_ENV, raising=False)
    monkeypatch.delenv("AGORA_OLLAMA_HOST", raising=False)
    monkeypatch.setattr(shutil, "which", _shim_which())
    monkeypatch.setattr(ollama_brain, "list_ollama_models", _dead_daemon("nope"))
    registry = BackendRegistry.from_yaml(_OLLAMA_ADAPTERS)

    assert cli_mod._doctor_brains(registry, "qwen") is False
    assert "UNREACHABLE (nope)" in capsys.readouterr().out


@requires_git
def test_doctor_warns_on_alphabetical_fallback_when_no_qwen(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """#96 crit 2: a qwen-less host still PASSES (the brain answers) but is told what it will get.

    The expected model is computed by calling the REAL ``select_model``: doctor must report the
    model a run would actually pick, and re-spelling the selection rule here would let the two
    drift silently — the whole reason the probe calls the shim's own functions.
    """
    target = _probe_repo(tmp_path / "kb", _OLLAMA_ADAPTERS)
    available = ["zephyr:7b", "llama3:8b", "mistral:7b"]
    monkeypatch.setattr(ollama_brain, "list_ollama_models", _installed_models(*available))
    monkeypatch.delenv(ollama_brain.MODEL_ENV, raising=False)
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert f"would use {ollama_brain.select_model(None, None, available)!r}" in out
    assert "3 models" in out
    assert "WARNING no qwen model installed" in out
    assert "alphabetical fallback" in out
    assert rc == 0


@requires_git
def test_doctor_pinned_model_skips_the_tags_probe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """#96 crit 3: an explicit ``--model`` short-circuits /api/tags — because the RUN does too.

    Listing models here would establish a fact the run never establishes. #129 draws the line one
    notch finer: the MODEL question is skipped, the LIVENESS question is not, because the run does
    ``POST /api/generate`` on this path like any other. Both halves are asserted together — a fix
    that started listing models to get reachability back would pass one and fail the other.
    """
    target = _probe_repo(
        tmp_path / "kb",
        "backends:\n"
        '  qwen: { argv: [agora-ollama-brain, --model, "pinned:1b"], network: loopback }\n'
        "default_backend: qwen\n",
    )
    calls: list[str] = []
    pings: list[str] = []

    def recorder(host: str, *, timeout: float) -> list[str]:
        calls.append(host)
        return ["whatever:1b"]

    monkeypatch.setattr(ollama_brain, "list_ollama_models", recorder)
    monkeypatch.setattr(ollama_brain, "ping_ollama", _live_ping(pings))
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert calls == []
    assert pings == ["http://localhost:11434"]
    assert "model pinned to 'pinned:1b' by adapters.yaml argv" in out
    # The parenthetical must not overclaim in EITHER direction: the daemon answered, but nothing
    # here says the pinned model is actually installed.
    assert "reachable, model pinned to 'pinned:1b'" in out
    assert "no /api/tags probe — the run lists no models either; the pin is NOT verified" in out
    assert rc == 0


@requires_git
def test_doctor_pinned_model_with_dead_daemon_is_unhealthy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """#129 case 1: a pinned repo whose daemon is DOWN must be RED, not green.

    The regression this locks is specific and was live on main: pinning the model in adapters.yaml
    argv made the probe skip /api/tags — and, before #129, skip reachability ENTIRELY — so doctor
    printed ``status: healthy`` for a repo where ``agora curate`` could not run at all. That is
    verbatim #96's opening complaint, reintroduced through the crit-3 short-circuit.

    The paired control (same repo, live daemon → healthy) is what proves the verdict tracks the
    daemon rather than the pin.
    """
    target = _probe_repo(
        tmp_path / "kb",
        "backends:\n"
        '  qwen: { argv: [agora-ollama-brain, --model, "pinned:1b"], network: loopback }\n'
        "default_backend: qwen\n",
    )
    monkeypatch.setattr(ollama_brain, "ping_ollama", _dead_ping("connection refused"))
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert "brain qwen: ollama http://localhost:11434 UNREACHABLE (connection refused)" in out
    assert "[model pinned to 'pinned:1b']" in out
    assert "status: unhealthy" in out
    assert rc == 1

    monkeypatch.setattr(ollama_brain, "ping_ollama", _live_ping())
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    assert "status: healthy" in capsys.readouterr().out
    assert rc == 0


@requires_git
def test_doctor_pinned_model_scheme_less_host_is_unhealthy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A portless ``$AGORA_OLLAMA_HOST`` makes urlopen raise a BARE ValueError, not a URLError.

    ``ping_ollama`` deliberately does not wrap it (neither does ``list_ollama_models``), so the
    pinned branch has to catch it exactly like the auto-select branch does. Without that catch the
    probe raises, doctor prints ``probe ERROR`` and offers no remediation — for what is the most
    ordinary first-run config typo there is.

    Deliberately does NOT use ``probe_env``: that fixture stubs ``ping_ollama``, and the REAL one
    is what this test is about. ``localhost`` (no scheme, no port) never leaves the machine —
    urlopen rejects it before opening a socket.
    """
    target = _probe_repo(
        tmp_path / "kb",
        "backends:\n"
        '  qwen: { argv: [agora-ollama-brain, --model, "pinned:1b"], network: loopback }\n'
        "default_backend: qwen\n",
    )
    monkeypatch.setattr(cli_mod, "_doctor_sandbox", lambda *a, **k: True)
    monkeypatch.setattr(shutil, "which", _shim_which())
    monkeypatch.delenv(ollama_brain.MODEL_ENV, raising=False)
    monkeypatch.setenv("AGORA_OLLAMA_HOST", "localhost")
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert "brain qwen: ollama localhost UNREACHABLE (ValueError:" in out
    assert "probe ERROR" not in out
    assert "status: unhealthy" in out
    assert rc == 1


# --- #129 case 2: does the sandbox confine THIS repo's brains? ----------------------------------


def _registry(adapters: str):  # type: ignore[no-untyped-def]
    from agora_kb.curator.backends import BackendRegistry

    return BackendRegistry.from_yaml(adapters)


_NONE_ADAPTERS = (
    "backends:\n  q: { argv: [agora-ollama-brain], network: none }\ndefault_backend: q\n"
)


# Two brains with opposite postures and NO `routing:` block, so which one is used is decided by
# the ADR-0015 repo-level precedence (`repo.yaml curator.backend`) rather than the registry default.
_TWO_BRAIN_ADAPTERS = (
    "backends:\n"
    "  loud: { argv: [agora-ollama-brain], network: loopback }\n"
    "  quiet: { argv: [agora-cli-brain], network: none }\n"
    "default_backend: loud\n"
)


def _split_adapters(plan: str, author: str) -> str:
    """The same two brains, routed per act. ``loud`` = loopback, ``quiet`` = none."""
    return f"{_TWO_BRAIN_ADAPTERS}routing: {{ plan: {plan}, author: {author} }}\n"


def test_sandbox_confinement_says_NO_for_the_default_loopback_repo() -> None:
    """`repo init` writes ``network: loopback``, so nothing in the default repo is confined.

    That is the misreading #129 exists to close — ``sandbox: seatbelt (ok)`` sits right above and
    nearly put a false confinement claim into SECURITY.md.
    """
    line = cli_mod._sandbox_confinement(_registry(_OLLAMA_ADAPTERS), "qwen", mechanism="seatbelt")

    assert line == (
        "confines this repo's brains: NO — outside: plan=qwen (network: loopback), "
        "author=qwen (network: loopback) "
        "(only a network: none author is confined; PASS-1 never is)"
    )


def test_sandbox_confinement_never_says_yes_because_PASS_1_is_never_confined() -> None:
    """Even an all-``network: none`` repo is only PARTIAL — ``plan`` never enters the sandbox.

    ``SubprocessBackend.plan`` passes ``confine=False`` unconditionally, so an unqualified "yes"
    is not true in ANY configuration. An earlier draft of this line printed one; that is the same
    class of overclaim #129 was filed to remove, one branch over.
    """
    line = cli_mod._sandbox_confinement(_registry(_NONE_ADAPTERS), "q", mechanism="seatbelt")

    assert line == (
        "confines this repo's brains: PARTIAL — confined: author=q (network: none); "
        "outside: plan=q (network: none) (PASS-1 is never confined on any path)"
    )
    assert "yes" not in line


def test_sandbox_confinement_reports_PARTIAL_when_the_AUTHOR_act_is_sandboxed() -> None:
    line = cli_mod._sandbox_confinement(
        _registry(_split_adapters(plan="loud", author="quiet")), "loud", mechanism="seatbelt"
    )

    assert line == (
        "confines this repo's brains: PARTIAL — confined: author=quiet (network: none); "
        "outside: plan=loud (network: loopback) (PASS-1 is never confined on any path)"
    )


def test_sandbox_confinement_says_NO_when_only_the_PLAN_act_is_network_none() -> None:
    """The inverted split: a ``network: none`` brain reached ONLY through ``routing.plan``.

    Nothing in this repo ever enters the sandbox, because PASS-1 is hard-coded ``confine=False``.
    A per-BACKEND reading of the same config says PARTIAL and names ``quiet`` as confined — which
    is flatly false and was the defect an adversarial review caught in the first draft.
    """
    line = cli_mod._sandbox_confinement(
        _registry(_split_adapters(plan="quiet", author="loud")), "loud", mechanism="seatbelt"
    )

    assert line == (
        "confines this repo's brains: NO — outside: plan=quiet (network: none), "
        "author=loud (network: loopback) "
        "(only a network: none author is confined; PASS-1 never is)"
    )
    assert "confined:" not in line


def test_sandbox_confinement_says_NO_when_the_host_has_no_kernel_sandbox() -> None:
    """``mechanism=None`` is ``SandboxUnavailable`` — and then the act cannot even be BUILT.

    Printing an affirmative directly beneath ``sandbox: unavailable`` would have the two adjacent
    lines contradict each other, with the operator acting on the stronger one.
    """
    line = cli_mod._sandbox_confinement(_registry(_NONE_ADAPTERS), "q", mechanism=None)

    assert line == (
        "confines this repo's brains: NO — no usable kernel sandbox on this host, so "
        "plan=q (network: none), author=q (network: none) cannot run at all "
        "('agora curate' fails closed)"
    )


def test_sandbox_confinement_does_not_count_the_restricted_fallback() -> None:
    """``allow_reduced_isolation`` selects a mechanism that confines neither writes nor egress."""
    line = cli_mod._sandbox_confinement(_registry(_NONE_ADAPTERS), "q", mechanism="restricted")

    assert line.startswith("confines this repo's brains: NO —")
    assert "the restricted fallback is not kernel confinement, ADR-0013" in line


def test_sandbox_confinement_does_not_count_a_sandbox_whose_selftest_failed() -> None:
    """A confinement that lies is worse than none — a FAILED self-test cannot yield `confined`."""
    line = cli_mod._sandbox_confinement(
        _registry(_NONE_ADAPTERS), "q", mechanism="seatbelt", proven=False
    )

    assert line.startswith("confines this repo's brains: NO —")
    assert "the sandbox self-test FAILED on this host" in line


def test_sandbox_confinement_is_silent_without_adapters_yaml() -> None:
    """No registry = nothing to say. Inventing a posture for a repo with no backend is worse than
    the missing line, and ``routing:`` already reports the absent file."""
    assert cli_mod._sandbox_confinement(None, "qwen", mechanism="seatbelt") is None


def test_sandbox_confinement_skips_a_backend_that_is_not_defined() -> None:
    """``repo.yaml`` naming an undefined brain is surfaced by ``routing:`` and the brain probe.

    Claiming anything about its confinement would be invention, and raising here would cost the
    operator the sandbox block that was already printed.
    """
    assert (
        cli_mod._sandbox_confinement(_registry(_OLLAMA_ADAPTERS), "ghost", mechanism="seatbelt")
        is None
    )


def test_print_sandbox_confinement_never_crashes_doctor(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An observability line is never a reason to lose the `sandbox:` block or `status:`."""

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli_mod, "_sandbox_confinement", boom)
    capsys.readouterr()

    cli_mod._print_sandbox_confinement(None, "qwen", mechanism="seatbelt")

    assert "confines this repo's brains: unknown (RuntimeError: kaboom)" in capsys.readouterr().out


# --- #129 third instance: a sandboxed act on a sandbox-less host is not `healthy` ----------------


def test_has_sandboxed_act_detects_either_act_and_never_raises() -> None:
    """Buildability, unlike confinement, turns on BOTH acts: ``_build_one`` selects isolation for
    any ``network: none`` spec, so a sandboxed PLAN act fails to build too."""
    assert cli_mod._has_sandboxed_act(_registry(_NONE_ADAPTERS), "q") is True
    assert cli_mod._has_sandboxed_act(_registry(_split_adapters("quiet", "loud")), "loud") is True
    assert cli_mod._has_sandboxed_act(_registry(_split_adapters("loud", "quiet")), "loud") is True
    assert cli_mod._has_sandboxed_act(_registry(_OLLAMA_ADAPTERS), "qwen") is False
    # Doctor's verdict must never turn RED on doctor's own uncertainty.
    assert cli_mod._has_sandboxed_act(None, "qwen") is False
    assert cli_mod._has_sandboxed_act(_registry(_OLLAMA_ADAPTERS), "ghost") is False


def test_doctor_sandbox_is_unhealthy_when_a_sandboxed_act_cannot_be_built(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#129's headline, third instance: green on a repo where ``agora curate`` cannot run.

    On a host with no kernel sandbox, ``build_routed_backend`` returns ``None`` for a
    ``network: none`` act and ``agora curate`` exits 1. Doctor used to return ``True`` here on the
    rationale that "the default loopback brain never needs a sandbox" — true for a loopback repo,
    false for this one. The control below is the loopback repo, which must stay green.
    """

    def unavailable(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise SandboxUnavailable("no bwrap, no userns")

    monkeypatch.setattr(cli_mod, "select_backend_isolation", unavailable)
    capsys.readouterr()

    assert (
        cli_mod._doctor_sandbox(False, registry=_registry(_NONE_ADAPTERS), default_backend="q")
        is False
    )
    out = capsys.readouterr().out
    assert "sandbox: unavailable" in out
    assert "cannot run at all ('agora curate' fails closed)" in out

    assert (
        cli_mod._doctor_sandbox(False, registry=_registry(_OLLAMA_ADAPTERS), default_backend="qwen")
        is True
    )

    # No registry at all = the pre-#129 behavior, unchanged: an inability to know is not a verdict.
    assert cli_mod._doctor_sandbox(False) is True


@requires_git
def test_doctor_prints_the_confinement_line_under_the_sandbox_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End of the wire: the line reaches real `agora doctor` output, and is REPORTING only.

    ``_doctor_sandbox`` is NOT stubbed here (that is what carries the line), so the assertion is on
    the confinement text alone — whether this host's kernel sandbox self-test passes is a property
    of the developer's machine and a different test's business.

    The repo defines TWO brains with opposite postures and points ``repo.yaml curator.backend`` at
    the one that is NOT the registry default, so the rendered line proves ``cfg.default_backend``
    (the ADR-0015 precedence the `routing:` line honours) actually reached the renderer. With a
    single brain named by both files, passing ``None`` there would be invisible.
    """
    target = _probe_repo(tmp_path / "kb", _TWO_BRAIN_ADAPTERS, backend="quiet")
    monkeypatch.setattr(shutil, "which", _shim_which())
    monkeypatch.setattr(ollama_brain, "list_ollama_models", _installed_models("qwen3.6:35b-a3b"))
    capsys.readouterr()

    main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    # `quiet` is repo.yaml's backend; `loud` is the registry default. Naming `quiet` is the proof
    # that the repo-level precedence reached the renderer — `loud` would print a NO line instead.
    assert "    confines this repo's brains: " in out
    assert "author=quiet (network: none)" in out
    assert "loud" not in out.split("confines this repo's brains: ")[1].split("\n")[0]


@requires_git
def test_doctor_prints_the_confinement_line_when_the_host_has_no_sandbox(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `SandboxUnavailable` branch carries the line too, and it is the one that matters most.

    Operators on a bwrap-less Linux box or native Windows (epic #85) are exactly the people who
    need to know nothing is confined, and macOS CI never executes that branch on its own. Without
    this test, deleting the call there would go unnoticed by a green suite.
    """
    target = _probe_repo(tmp_path / "kb", _NONE_ADAPTERS, backend="q")

    def unavailable(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise SandboxUnavailable("no usable sandbox")

    monkeypatch.setattr(cli_mod, "select_backend_isolation", unavailable)
    monkeypatch.setattr(shutil, "which", _shim_which())
    monkeypatch.setattr(ollama_brain, "list_ollama_models", _installed_models("qwen3.6:35b-a3b"))
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert "  sandbox: unavailable" in out
    assert "confines this repo's brains: NO — no usable kernel sandbox on this host" in out
    assert "cannot run at all ('agora curate' fails closed)" in out
    assert "status: unhealthy" in out
    assert rc == 1


@requires_git
def test_doctor_env_pinned_model_still_contacts_the_daemon(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """The inverse of crit 3, and a real asymmetry: an ENV pin does NOT skip the /api/tags call.

    ``_resolve_model`` evaluates ``list_ollama_models(host)`` as an ARGUMENT to ``select_model``,
    so a down daemon fails a run even with $AGORA_OLLAMA_MODEL set — and doctor reproduces that
    rather than quietly reporting a model the run could never reach.
    """
    target = _probe_repo(tmp_path / "kb", _OLLAMA_ADAPTERS)
    called: list[str] = []

    def recorder(host: str, *, timeout: float) -> list[str]:
        called.append(host)
        return ["llama3:8b"]

    monkeypatch.setattr(ollama_brain, "list_ollama_models", recorder)
    monkeypatch.setenv(ollama_brain.MODEL_ENV, "custom:1b")
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert called == ["http://localhost:11434"]
    assert "would use 'custom:1b' (pinned by $AGORA_OLLAMA_MODEL)" in out
    assert "WARNING no qwen" not in out  # an explicit pin is not an accidental fallback
    assert rc == 0


@requires_git
def test_doctor_reports_a_missing_non_ollama_brain_program(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], probe_env: None
) -> None:
    """#96 crit 4: a configured brain whose argv[0] is on no PATH is a RED verdict, not a surprise
    at the first curate run."""
    target = _probe_repo(
        tmp_path / "kb",
        "backends:\n  custom: { argv: [definitely-not-installed-xyz] }\ndefault_backend: custom\n",
        backend="custom",
    )
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert (
        "  brain custom: 'definitely-not-installed-xyz' NOT FOUND on PATH — "
        "install it or fix the adapters.yaml argv" in out
    )
    assert rc == 1


@requires_git
def test_doctor_reports_a_present_non_ollama_brain_program(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], probe_env: None
) -> None:
    """A resolvable, executable argv[0] passes and names the path a spawn would use."""
    target = _probe_repo(
        tmp_path / "kb",
        f"backends:\n  custom: {{ argv: [{sys.executable!r}, -c, pass] }}\n"
        "default_backend: custom\n",
        backend="custom",
    )
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert f"on PATH ({sys.executable})" in out
    assert rc == 0


@requires_git
def test_doctor_reports_a_missing_ollama_shim_before_probing_the_daemon(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """#96 crit 4 applies to the OLLAMA shim too — the one backend `repo init` actually writes.

    The reported first-run shape: `pip install --user` puts ``agora-ollama-brain`` in a bin dir
    that is not on the curator's PATH while Ollama itself is up and healthy. `agora curate` then
    dies with ``BackendUnavailableError`` at execvp — so a daemon-only probe answering `healthy`
    is verbatim the issue's opening complaint. The daemon must not even be contacted: the spawn
    could never happen.
    """
    target = _probe_repo(
        tmp_path / "kb",
        "backends:\n  qwen: { argv: [agora-ollama-brain], network: loopback }\n"
        "default_backend: qwen\n",
    )
    calls: list[str] = []

    def recorder(host: str, *, timeout: float) -> list[str]:
        calls.append(host)
        return ["qwen3.6:35b-a3b"]

    monkeypatch.setattr(ollama_brain, "list_ollama_models", recorder)
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)  # nothing at all resolves
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert (
        "  brain qwen: 'agora-ollama-brain' NOT FOUND on PATH — "
        "install it or fix the adapters.yaml argv" in out
    )
    assert calls == []  # an unspawnable brain's daemon is not the question
    assert "fix (no download" in out  # the FAIL still earns a remediation block
    assert "status: unhealthy" in out
    assert rc == 1


@requires_git
def test_doctor_does_not_probe_a_worktree_placeholder_argv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], probe_env: None
) -> None:
    """The ``{worktree}`` verdict-matrix row: unprobeable (the dir does not exist until a run) is
    an inability to probe, and the governing rule never fails a verdict on one."""
    target = _probe_repo(
        tmp_path / "kb",
        "backends:\n  custom: { argv: ['{worktree}/bin/brain'] }\ndefault_backend: custom\n",
        backend="custom",
    )
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert (
        "  brain custom: not probed (argv[0] '{worktree}/bin/brain' is resolved per-run "
        "from {worktree})" in out
    )
    assert "fix (" not in out
    assert rc == 0


@requires_git
def test_doctor_reports_a_resolvable_but_non_executable_brain_program(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], probe_env: None
) -> None:
    """The ``NOT EXECUTABLE`` row — the ONLY thing between a path-ish argv[0] and a false PASS.

    ``resolve_program_on_path`` returns a path-ish argv[0] UNCHANGED without touching the
    filesystem (that is what the spawn does too), so without the ``is_file() and X_OK`` check a
    mode-0644 file would report `on PATH` and fail at execvp instead.
    """
    brain = tmp_path / "brain-not-executable"
    brain.write_text("#!/bin/sh\n", encoding="utf-8")
    brain.chmod(0o644)
    target = _probe_repo(
        tmp_path / "kb",
        f"backends:\n  custom: {{ argv: [{str(brain)!r}] }}\ndefault_backend: custom\n",
        backend="custom",
    )
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert f"  brain custom: '{brain}' at {brain} is NOT EXECUTABLE — " in out
    assert "fix (no download" in out
    assert rc == 1


@requires_git
def test_doctor_scheme_less_ollama_host_reads_as_unreachable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], probe_env: None
) -> None:
    """A host with NO url scheme at all (``--host ollama-box``) is a first-run config typo, not a
    bug.

    ``urlopen`` raises a bare ``ValueError`` when it cannot find a url type, and
    ``list_ollama_models`` wraps only URLError/OSError/JSONDecodeError — so this used to land on
    the ``probe ERROR`` branch, which reads as an internal defect and, by design, prints NO
    remediation. (``localhost:11434`` is NOT this case: urlopen reads ``localhost`` as the scheme
    and fails with a URLError the shim already wraps.)
    """
    target = _probe_repo(
        tmp_path / "kb",
        "backends:\n  qwen: { argv: [agora-ollama-brain, --host, ollama-box] }\n"
        "default_backend: qwen\n",
    )
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert "  brain qwen: ollama ollama-box UNREACHABLE (ValueError: " in out
    assert "probe ERROR" not in out
    assert "fix (no download" in out
    assert rc == 1


@requires_git
def test_doctor_survives_a_remediation_block_that_raises(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """The fix HINT sits outside the probe's own guard, so it carries its own (A1.5).

    A hint that cannot be built is a footnote; losing the ``status:`` line over it would defeat the
    whole point of a diagnostic command.
    """
    target = _probe_repo(tmp_path / "kb", _OLLAMA_ADAPTERS)
    monkeypatch.setattr(ollama_brain, "list_ollama_models", _dead_daemon())

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("which exploded")

    monkeypatch.setattr(cli_mod, "_print_brain_remediation", boom)
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    captured = capsys.readouterr()
    assert "  brain qwen: ollama http://localhost:11434 UNREACHABLE (boom)" in captured.out
    assert "    fix: unavailable (RuntimeError: which exploded)" in captured.out
    assert "status: unhealthy" in captured.out
    assert "Traceback" not in captured.err
    assert rc == 1


@requires_git
def test_doctor_probe_passes_a_bounded_timeout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """#96 crit 5: the probe is time-BOUNDED, and by a budget an operator is willing to wait on.

    A blackholed daemon (SYN dropped, not refused) would otherwise hang doctor for the shim's 10s
    runtime default per routed brain.
    """
    target = _probe_repo(tmp_path / "kb", _OLLAMA_ADAPTERS)
    recorded: list[float] = []

    def recorder(host: str, *, timeout: float) -> list[str]:
        recorded.append(timeout)
        return ["qwen3.6:35b-a3b"]

    monkeypatch.setattr(ollama_brain, "list_ollama_models", recorder)
    capsys.readouterr()

    assert main(["doctor", "--repo", str(target)]) == 0
    assert recorded == [3.0]
    assert cli_mod._BRAIN_PROBE_TIMEOUT_S == 3.0


@requires_git
def test_doctor_skip_probe_bypasses_the_probe_entirely(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """#96 crit 5: ``--skip-probe`` makes NO daemon or PATH lookup at all (the brain-less node /
    CI escape hatch), and the verdict then simply ignores brain reachability."""

    def must_not_run(host: str, *, timeout: float) -> list[str]:
        raise AssertionError("probe must not run")

    target = _probe_repo(tmp_path / "kb", _OLLAMA_ADAPTERS)
    monkeypatch.setattr(ollama_brain, "list_ollama_models", must_not_run)
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target), "--skip-probe"])

    out = capsys.readouterr().out
    assert "  brains: probe skipped (--skip-probe)" in out
    assert rc == 0


def test_doctor_brains_line_when_no_adapters_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No adapters.yaml = nothing configured to probe: reported, never a failed verdict."""
    main(["doctor", "--repo", str(tmp_path)])
    out = capsys.readouterr().out
    assert "  brains: not probed (no adapters.yaml — no backend configured)" in out


@requires_git
def test_doctor_brains_unknown_backend_is_unhealthy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """A repo.yaml ``curator.backend`` naming no defined brain FAILS: that is an established fact,
    not an inability to probe — ``agora curate`` cannot run at all in this repo."""
    target = _probe_repo(tmp_path / "kb", _OLLAMA_ADAPTERS, backend="ghost")
    monkeypatch.setattr(ollama_brain, "list_ollama_models", _dead_daemon("must not be called"))
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert "plan=ghost (UNKNOWN backend)" in out
    assert (
        "  brain ghost: UNKNOWN backend — not defined in adapters.yaml; "
        "'agora curate' cannot run" in out
    )
    assert rc == 1


@requires_git
def test_doctor_brains_dedupes_and_orders_by_act(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], probe_env: None
) -> None:
    """One line per DISTINCT routed brain, in act order (plan, then author) — not one per act.

    Both acts on one brain must probe once (a two-line report would imply two round trips); two
    brains must appear in a deterministic order, which is what makes the report test-lockable.
    """
    two = (
        "backends:\n"
        f"  a: {{ argv: [{sys.executable!r}] }}\n"
        f"  b: {{ argv: [{sys.executable!r}] }}\n"
        "routing:\n  plan: a\n  author: b\n"
        "default_backend: a\n"
    )
    target = _probe_repo(tmp_path / "kb", two, backend="a")
    capsys.readouterr()

    main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.lstrip().startswith("brain ")]
    assert len(lines) == 2
    assert lines[0].lstrip().startswith("brain a:")
    assert lines[1].lstrip().startswith("brain b:")

    (target / "adapters.yaml").write_text(two.replace("author: b", "author: a"), encoding="utf-8")
    capsys.readouterr()

    main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert len([ln for ln in out.splitlines() if ln.lstrip().startswith("brain ")]) == 1


@requires_git
def test_doctor_brains_zero_models_is_unhealthy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """A daemon that answers but has pulled NOTHING is still a curator that cannot run."""
    target = _probe_repo(tmp_path / "kb", _OLLAMA_ADAPTERS)
    monkeypatch.setattr(ollama_brain, "list_ollama_models", _installed_models())
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert (
        "  brain qwen: ollama http://localhost:11434 reachable, 0 models installed "
        "(the curator has no model to run)" in out
    )
    assert rc == 1


@requires_git
def test_doctor_brains_zero_models_with_env_pin_passes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """``select_model`` honors the env pin BEFORE the empty-list guard, so an empty /api/tags with
    $AGORA_OLLAMA_MODEL set is a run that proceeds — doctor must not contradict it. The pin is
    reported as unverified: /api/tags is not a sound existence test for an unqualified name."""
    target = _probe_repo(tmp_path / "kb", _OLLAMA_ADAPTERS)
    monkeypatch.setattr(ollama_brain, "list_ollama_models", _installed_models())
    monkeypatch.setenv(ollama_brain.MODEL_ENV, "custom:1b")
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert (
        "reachable, 0 models installed, would use 'custom:1b' "
        "(pinned by $AGORA_OLLAMA_MODEL — NOT installed here)" in out
    )
    assert rc == 0


@requires_git
def test_doctor_brains_never_crashes_on_an_unexpected_probe_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """An unexpected exception inside the probe is REPORTED and fails the verdict (a probe that
    cannot answer is not a healthy host), but never tracebacks out of doctor — and prints no
    remediation, because a fix for an internal defect would be a guess."""
    target = _probe_repo(tmp_path / "kb", _OLLAMA_ADAPTERS)

    def boom(host: str, *, timeout: float) -> list[str]:
        return [1 / 0]  # type: ignore[list-item]

    monkeypatch.setattr(ollama_brain, "list_ollama_models", boom)
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    captured = capsys.readouterr()
    assert "  brain qwen: probe ERROR — ZeroDivisionError" in captured.out
    assert "fix (" not in captured.out
    assert "Traceback" not in captured.err
    assert rc == 1


@requires_git
def test_doctor_identifies_the_shim_via_python_m(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """The ``python -m agora_kb.adapters.ollama_brain`` form is the same shim under any interpreter
    path, so its own ``--host`` must be read by the shim's own parser."""
    target = _probe_repo(
        tmp_path / "kb",
        "backends:\n"
        f"  qwen: {{ argv: [{sys.executable!r}, -m, {ollama_brain.__name__!r}, "
        '--host, "http://elsewhere:1"] }\n'
        "default_backend: qwen\n",
    )
    seen: list[str] = []

    def recorder(host: str, *, timeout: float) -> list[str]:
        seen.append(host)
        return ["qwen3.6:35b-a3b"]

    monkeypatch.setattr(ollama_brain, "list_ollama_models", recorder)
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert seen == ["http://elsewhere:1"]
    assert "  brain qwen: ollama http://elsewhere:1 reachable, 1 models, would use " in out
    assert rc == 0


@pytest.mark.parametrize(
    "program",
    ["agora-ollama-brain.exe", "Agora-Ollama-Brain.exe", "AGORA-OLLAMA-BRAIN.EXE"],
)
def test_ollama_argv_tail_matches_the_windows_shim_case_insensitively(program: str) -> None:
    """Windows' filesystem is case-insensitive, so a differently-cased ``.exe`` is the SAME shim.

    A case-sensitive match would silently drop the Windows leg (epic #85) to the generic PATH
    probe, losing the ``--model``/``--host`` reporting that criteria 2 and 3 are about.
    """
    assert cli_mod._ollama_argv_tail((program, "--model", "x")) == ("--model", "x")
    assert cli_mod._ollama_argv_tail(("something-else.exe", "--model", "x")) is None


@requires_git
def test_doctor_wrapper_argv_degrades_to_the_path_probe(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """A WRAPPED shim (``uv run agora-ollama-brain --model x``) is probed as a plain program.

    The flags after a wrapper belong to the WRAPPER, so feeding them to the shim's parser would
    report a model the run never uses — a confident wrong answer, strictly worse than the honest
    generic argv[0]-on-PATH fallback.
    """
    target = _probe_repo(
        tmp_path / "kb",
        "backends:\n"
        "  qwen: { argv: [uv, run, agora-ollama-brain, --model, x], network: loopback }\n"
        "default_backend: qwen\n",
    )

    def must_not_run(host: str, *, timeout: float) -> list[str]:
        raise AssertionError("a wrapped argv must not be parsed as the shim's own")

    monkeypatch.setattr(ollama_brain, "list_ollama_models", must_not_run)
    capsys.readouterr()

    main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert "  brain qwen: 'uv'" in out  # the WRAPPER is what gets probed
    assert "model pinned" not in out


@requires_git
def test_doctor_unparseable_shim_argv_leaks_no_usage_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], probe_env: None
) -> None:
    """A typo'd/hostile shim argv is reported, and argparse's own exit path leaks NO bytes.

    ``parse_known_args`` writes usage to stderr and exits on a malformed known flag; that output
    must never land in a health report a machine or an operator reads.
    """
    target = _probe_repo(
        tmp_path / "kb",
        "backends:\n  qwen: { argv: [agora-ollama-brain, --model] }\ndefault_backend: qwen\n",
    )
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    captured = capsys.readouterr()
    assert (
        "  brain qwen: ollama shim argv UNPARSEABLE by the shim's own parser — "
        "check the adapters.yaml argv" in captured.out
    )
    assert "usage: agora-ollama-brain" not in captured.out
    assert "usage: agora-ollama-brain" not in captured.err
    assert rc == 1


@requires_git
def test_doctor_shim_help_flag_in_argv_prints_no_help(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], probe_env: None
) -> None:
    """``-h`` in a configured argv exits argparse with 0 after printing HELP TO STDOUT — the one
    exit path that would otherwise inject a whole help screen into doctor's report."""
    target = _probe_repo(
        tmp_path / "kb",
        "backends:\n  qwen: { argv: [agora-ollama-brain, -h] }\ndefault_backend: qwen\n",
    )
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    captured = capsys.readouterr()
    assert "UNPARSEABLE by the shim's own parser" in captured.out
    assert "show this help message" not in captured.out
    assert "show this help message" not in captured.err
    assert rc == 1


@requires_git
def test_doctor_survives_a_malformed_repo_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`agora doctor` must ALWAYS reach its ``status:`` line — a YAML typo in repo.yaml is exactly
    the state an operator runs doctor in, and before #96 it tracebacked out instead (leaving the
    verdict unreachable). `agora curate` still fails loudly on the same file."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--domain", "ai-tech"]) == 0
    repo_yaml = RepoLayout(target).kb_dir / "repo.yaml"
    repo_yaml.write_text(repo_yaml.read_text(encoding="utf-8") + "\ncron: @daily\n", "utf-8")
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target), "--skip-probe"])

    captured = capsys.readouterr()
    assert "  repo.yaml: unreadable (" in captured.out
    assert ") — using defaults" in captured.out
    assert "status:" in captured.out
    assert "Traceback" not in captured.err
    assert rc in (0, 1)

    # A file that is not even UTF-8 — a CP949/Windows editor writing a Korean `name:` (epic #85 /
    # issue #57). `load_repo_config` wraps only yaml.YAMLError, so this raises UnicodeDecodeError
    # straight through the ConfigError guard; doctor must still reach its verdict line.
    repo_yaml.write_bytes(b"name: kb\n\xff\xfe: bad\n")
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target), "--skip-probe"])

    captured = capsys.readouterr()
    assert "  repo.yaml: unreadable (" in captured.out
    assert "status:" in captured.out
    assert "Traceback" not in captured.err
    assert rc in (0, 1)


# --- doctor: the tool-agnostic brain remediation block (owner ruling A1) --------------------------
@requires_git
def test_doctor_remediation_names_an_installed_cli_agent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """The CHEAP recovery comes first: a headless CLI agent already on PATH, wired via ADR-0016's
    ``agora-cli-brain``. Telling a beta user to pull a ~20 GB model is the most expensive fix
    available, so Ollama is retained but demoted to "instead"."""
    target = _probe_repo(tmp_path / "kb", _OLLAMA_ADAPTERS)
    monkeypatch.setattr(ollama_brain, "list_ollama_models", _dead_daemon())
    monkeypatch.setattr(shutil, "which", _which_only({"claude"}))
    capsys.readouterr()

    main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert "    fix (no download — 'claude' is already installed): add to adapters.yaml" in out
    assert "        claude: { argv: [agora-cli-brain, --, claude, -p], network: loopback }" in out
    assert "      then set  curator.backend: claude  in _kb/repo.yaml (ADR-0016) — i.e." in out
    assert "    fix (local model instead): ollama serve  &&  ollama pull qwen3.6:35b-a3b" in out
    assert out.index("fix (no download") < out.index("ollama serve")


@requires_git
def test_doctor_remediation_snippet_is_actually_paste_able(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """FOLLOWING the advice must produce a repo the loaders accept — the whole point of A1.4.

    The entries alone are not paste-able: ``adapters.yaml`` nests backends under ``backends:`` and
    ``repo.yaml`` nests the default under ``curator:``. A bare entry appended verbatim makes
    adapters.yaml UNPARSEABLE, and a literal top-level ``curator.backend:`` key is silently ignored
    by ``load_repo_config`` — so the beta user reruns doctor, sees the identical FAIL, and has no
    signal that their edit did nothing. This drives the emitted block through the real loaders.
    """
    target = _probe_repo(tmp_path / "kb", _OLLAMA_ADAPTERS)
    monkeypatch.setattr(ollama_brain, "list_ollama_models", _dead_daemon())
    monkeypatch.setattr(shutil, "which", _which_only({"claude"}))
    capsys.readouterr()

    main(["doctor", "--repo", str(target)])

    block = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("      ")]
    # The two YAML fragments the operator pastes, minus the prose line that joins them. Dedented
    # by the block's own 6-space report indent, which is what a copy-paste drops.
    adapters_snippet = textwrap.dedent("\n".join(block[:2]))
    repo_snippet = textwrap.dedent("\n".join(block[3:5]))

    # adapters.yaml: through the REAL loader, not just a YAML parse — `backends:` is the key the
    # registry reads, and an entry pasted without it is silently ignored.
    pasted = tmp_path / "pasted-adapters.yaml"
    pasted.write_text(f"{adapters_snippet}\ndefault_backend: claude\n", encoding="utf-8")
    assert load_backend_registry(pasted).get("claude").argv == (
        "agora-cli-brain",
        "--",
        "claude",
        "-p",
    )
    # repo.yaml: the shorthand `curator.backend` is TWO nested keys — a literal dotted top-level
    # key parses fine and is then dropped on the floor by load_repo_config.
    assert yaml.safe_load(repo_snippet) == {"curator": {"backend": "claude"}}


@requires_git
def test_doctor_remediation_without_any_installed_agent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """With no known agent on PATH the advice stays tool-agnostic — a list, not one vendor."""
    target = _probe_repo(tmp_path / "kb", _OLLAMA_ADAPTERS)
    monkeypatch.setattr(ollama_brain, "list_ollama_models", _dead_daemon())
    monkeypatch.setattr(shutil, "which", _which_only(set()))
    capsys.readouterr()

    main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert (
        "    fix (no download): install a headless CLI agent (claude, codex, gemini) "
        "and drive it via" in out
    )
    assert "      agora-cli-brain — see ADR-0016" in out
    assert "    fix (local model instead): ollama serve  &&  ollama pull qwen3.6:35b-a3b" in out


@requires_git
def test_doctor_remediation_lists_all_installed_agents(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """Every installed agent is named (the operator picks), while the copy-pasteable snippet uses
    the first — one snippet, no menu of near-identical YAML blocks."""
    target = _probe_repo(tmp_path / "kb", _OLLAMA_ADAPTERS)
    monkeypatch.setattr(ollama_brain, "list_ollama_models", _dead_daemon())
    monkeypatch.setattr(shutil, "which", _which_only({"claude", "codex"}))
    capsys.readouterr()

    main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert "fix (no download — 'claude', 'codex' are already installed)" in out
    assert "      claude: { argv: [agora-cli-brain, --, claude, -p], network: loopback }" in out


@requires_git
def test_doctor_remediation_is_printed_once_per_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], probe_env: None
) -> None:
    """Two dead brains produce two diagnoses but ONE fix block — the advice is about the repo, not
    about each backend, and repeating it is noise in the report an operator is trying to read."""
    target = _probe_repo(
        tmp_path / "kb",
        "backends:\n"
        "  a: { argv: [definitely-not-installed-xyz-a] }\n"
        "  b: { argv: [definitely-not-installed-xyz-b] }\n"
        "routing:\n  plan: a\n  author: b\n"
        "default_backend: a\n",
        backend="a",
    )
    capsys.readouterr()

    rc = main(["doctor", "--repo", str(target)])

    out = capsys.readouterr().out
    assert len([ln for ln in out.splitlines() if ln.lstrip().startswith("brain ")]) == 2
    assert out.count("fix (no download") == 1
    assert rc == 1


@requires_git
def test_doctor_healthy_run_prints_no_remediation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """A usable brain gets a one-line PASS and nothing else: remediation is for FAILURES only."""
    target = _probe_repo(tmp_path / "kb", _OLLAMA_ADAPTERS)
    monkeypatch.setattr(ollama_brain, "list_ollama_models", _installed_models("qwen3.6:35b-a3b"))
    capsys.readouterr()

    assert main(["doctor", "--repo", str(target)]) == 0
    assert "fix (" not in capsys.readouterr().out


def test_known_cli_agents_match_the_module_docstring() -> None:
    """The ADR-0016 invocations exist ONCE, as data. The shim's docstring table drifted from them
    before (codex's flags lived only in DATA-MODEL.md §8) and doctor's hint now renders from the
    same tuple, so this pins the doc to the data rather than to a reviewer's memory."""
    doc = cli_agent_brain.__doc__ or ""
    for name, argv in cli_agent_brain.KNOWN_CLI_AGENTS:
        assert f"{name}:" in doc
        assert f"argv: {cli_mod._argv_yaml(argv)}" in doc


def test_known_cli_agents_match_the_data_model_table() -> None:
    """The THIRD copy: ``docs/DATA-MODEL.md`` §8's adapters.yaml example.

    It is the copy that drifted last time (it carried codex's flags while the shim docstring did
    not), and "one source of truth" is only true while every copy is locked to it.
    """
    doc = (Path(__file__).resolve().parents[1] / "docs" / "DATA-MODEL.md").read_text(
        encoding="utf-8"
    )
    for name, argv in cli_agent_brain.KNOWN_CLI_AGENTS:
        assert f"argv: {cli_mod._argv_yaml(argv)}" in doc, name


# --- serve --------------------------------------------------------------------------------------
def test_serve_invokes_build_server_with_repo_path_and_default_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Monkeypatch build_server with a recording stub returning an object whose .run is a no-op,
    # so we exercise _cmd_serve's call shape without blocking on the stdio loop.
    import agora_kb.faces.mcp_server as mcp_server

    captured: dict[str, object] = {}

    class _StubServer:
        def run(self) -> None:  # no-op: never blocks
            captured["ran"] = True

    def _fake_build_server(
        *, repo_path: Path, writer: str = mcp_server.DEFAULT_WRITER
    ) -> _StubServer:
        captured["repo_path"] = repo_path
        captured["writer"] = writer
        return _StubServer()

    monkeypatch.setattr(mcp_server, "build_server", _fake_build_server)

    rc = main(["serve", "--repo", str(tmp_path)])
    assert rc == 0
    # repo_path is forwarded as a Path of the expected value.
    assert isinstance(captured["repo_path"], Path)
    assert captured["repo_path"] == Path(str(tmp_path))
    # --writer omitted: build_server's own default ('local') applies, never None.
    assert captured["writer"] == mcp_server.DEFAULT_WRITER
    assert captured["ran"] is True


def test_serve_forwards_explicit_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agora_kb.faces.mcp_server as mcp_server

    captured: dict[str, object] = {}

    class _StubServer:
        def run(self) -> None:
            captured["ran"] = True

    def _fake_build_server(
        *, repo_path: Path, writer: str = mcp_server.DEFAULT_WRITER
    ) -> _StubServer:
        captured["repo_path"] = repo_path
        captured["writer"] = writer
        return _StubServer()

    monkeypatch.setattr(mcp_server, "build_server", _fake_build_server)

    rc = main(["serve", "--repo", str(tmp_path), "--writer", "agent-7"])
    assert rc == 0
    assert isinstance(captured["repo_path"], Path)
    assert captured["writer"] == "agent-7"
    assert captured["ran"] is True


# --- watch (scheduler loop) ---------------------------------------------------------------------
def _init_stub_repo(target: Path) -> RepoLayout:
    """Init a repo with the stub brain wired (domain/tags matching the stub plan) → its layout."""
    assert (
        main(
            [
                "repo",
                "init",
                str(target),
                "--domain",
                "ai-tech",
                "--tag",
                "curator",
                "--tag",
                "concurrency",
            ]
        )
        == 0
    )
    _write_stub_adapters(target)
    layout = RepoLayout(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("backend: qwen", "backend: stub"),
        encoding="utf-8",
    )
    return layout


class _FakeSleeper:
    """Records the sleep schedule; ends the loop with a Ctrl-C after ``stop_after`` calls.

    The `agora watch` loop is otherwise unbounded, so #97's backoff/reset behaviour is only
    observable through the one indirection that owns ``time.sleep`` (``cli._watch_sleep``). Raising
    ``KeyboardInterrupt`` from the sleeper is the honest terminator: it is exactly how an operator
    stops the daemon, so the tests drive the real exit path instead of a synthetic one.

    ``on_call`` fires BEFORE recording, so a test can repair the repo mid-loop.
    """

    def __init__(self, stop_after: int, on_call=None) -> None:  # type: ignore[no-untyped-def]
        self.calls: list[float] = []
        self._stop_after, self._on_call = stop_after, on_call

    def __call__(self, seconds: float) -> None:
        if self._on_call is not None:
            self._on_call(len(self.calls))
        self.calls.append(seconds)
        if len(self.calls) >= self._stop_after:
            raise KeyboardInterrupt


def _fail_if_slept(seconds: float) -> None:
    """A ``_watch_sleep`` stand-in for the paths that must never reach a sleep (``--once``)."""
    raise AssertionError(f"_watch_sleep must not be called (got {seconds})")


@requires_git
def test_watch_once_is_idle_on_empty_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`watch --once` on an empty repo evaluates the triggers, finds nothing due, and exits 0."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    out = capsys.readouterr().out
    assert "idle: depth=0" in out
    assert "reason=none" in out


@requires_git
def test_watch_once_runs_when_threshold_met(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A backlog at/above ``threshold`` makes `watch --once` consolidate (reason=threshold)."""
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("threshold: 10", "threshold: 1"),
        encoding="utf-8",
    )
    capsys.readouterr()
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    out = capsys.readouterr().out
    assert "ran (threshold)" in out
    assert "status=published" in out


@requires_git
def test_watch_tick_reports_a_failed_run_cause(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The #96 failure lines ride the tick log's ``{stamp} <verb>:`` grammar, so
    `journalctl -u agora-watch` stays ONE stream — and a failed RUN is still a successful TICK.

    An always-on watch loop is precisely where a repeatedly-failing curator would otherwise
    accumulate `status=failed` with no way to reach the cause. The rc assertion is the #97 boundary
    in miniature: the run failed, the tick did not raise, so the scheduler exits 0.
    """
    target = tmp_path / "kb"
    layout = _dead_brain_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("threshold: 10", "threshold: 1"),
        encoding="utf-8",
    )
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    lines = capsys.readouterr().out.splitlines()
    ran = [ln for ln in lines if " ran (" in ln]
    records = [ln for ln in lines if " failed_record: " in ln]
    checks = [ln for ln in lines if " failed_checks: " in ln]
    assert len(ran) == 1
    assert len(records) == 1
    assert len(checks) == 1
    assert "status=failed" in ran[0]
    # Same stamp as the `ran (...)` line it explains — the lines are one event, not three.
    stamp = ran[0].split(" ", 1)[0]
    assert records[0].startswith(f"{stamp} failed_record: _kb/failed/")
    assert checks[0].startswith(f"{stamp} failed_checks: PLAN-BACKEND")


@requires_git
def test_watch_threads_curator_thresholds_into_worker_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The `agora watch` tick reads curator.limits.related_k / curator.lint.max_orphans from
    repo.yaml and forwards them into worker.run — locks the face glue, not just the worker seam."""
    import agora_kb.cli as cli_mod

    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    doc = yaml.safe_load(repo_yaml.read_text(encoding="utf-8"))
    doc["curator"]["triggers"]["threshold"] = 1  # one backlog item makes the tick consolidate
    doc["curator"]["limits"] = {"related_k": 5}
    doc["curator"]["lint"] = {"max_orphans": 7}
    repo_yaml.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )

    seen: dict[str, object] = {}
    orig_run = cli_mod.run

    def run_spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return orig_run(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "run", run_spy)
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0
    assert seen.get("related_k") == 5  # the watch face forwarded repo.yaml's related_k
    assert seen.get("max_orphans") == 7  # ...and max_orphans


@requires_git
def test_watch_once_runs_on_cron_due(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A cron matching every minute fires `watch --once` (reason=cron) with a backlog present."""
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    # cron "* * * * *" is due every minute; threshold stays 10 so ONLY the cron signal can fire.
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("cron: 0 3 * * *", "cron: '* * * * *'"),
        encoding="utf-8",
    )
    capsys.readouterr()
    # Current-timestamp event so the 30-min idle trigger cannot pre-empt the cron reason.
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime.now(UTC),
    )

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    out = capsys.readouterr().out
    assert "ran (cron)" in out
    assert "status=published" in out


# --- watch: the #97 tick guard + backoff ---------------------------------------------------------
# One `{stamp} tick failed: <Type>: <detail>` line per failed tick, on stderr.
_TICK_FAILED_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ tick failed: ", re.MULTILINE)


@pytest.fixture()
def no_watch_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    """`AGORA_WATCH_TRACEBACK` is a HOST env var — unset it so "no traceback" asserts are real."""
    monkeypatch.delenv("AGORA_WATCH_TRACEBACK", raising=False)


@requires_git
@pytest.mark.usefixtures("no_watch_traceback")
def test_watch_survives_a_corrupt_state_json_without_dying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#97 criterion 1: a DETERMINISTIC per-tick raise reports and backs off — it never exits.

    A corrupt ``_kb/state.json`` is fail-loud by design (``StateStore.load`` never silently
    discards ``published_runs``/``event_keys``), so ``recover()`` raises on EVERY tick. Before the
    guard this exited non-zero into `Restart=on-failure`/`KeepAlive`, converting a 60s scheduler
    into a 10s crash loop that could never fix the file.
    """
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    layout.state_file.parent.mkdir(parents=True, exist_ok=True)
    layout.state_file.write_text("not json at all", encoding="utf-8")
    sleeper = _FakeSleeper(stop_after=3)
    monkeypatch.setattr(cli_mod, "_watch_sleep", sleeper)
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--interval", "1"]) == 0

    captured = capsys.readouterr()
    err = captured.err
    assert len(_TICK_FAILED_RE.findall(err)) == 3  # three ticks, three reports, no exit
    assert "tick failed: ValidationError: " in err
    assert "1 validation error for CuratorState" in err
    # The DIAGNOSIS is on pydantic's line 2 — this is the assertion that fails if the renderer ever
    # truncates at the first newline instead of collapsing whitespace (_one_line).
    assert "Invalid JSON" in err
    assert "Traceback (most recent call last)" not in err
    assert "agora watch: stopped" in captured.out


@requires_git
@pytest.mark.usefixtures("no_watch_traceback")
def test_watch_recovers_and_resets_backoff_after_the_repo_is_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#97 criterion 2: the FIRST clean tick after a repair resets the backoff to ``interval``.

    ``on_call(n)`` runs during sleep ``n+1``, i.e. AFTER tick ``n+1`` has already computed its
    delay — so repairing at ``n == 1`` makes tick 3 the first clean one and yields [2, 4, 1, 1].
    """
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    layout.state_file.parent.mkdir(parents=True, exist_ok=True)
    layout.state_file.write_text("not json at all", encoding="utf-8")

    def repair(n: int) -> None:
        if n == 1:  # during the 2nd sleep: ticks 1-2 failed, tick 3 will be clean
            layout.state_file.unlink()  # a missing state.json is a fresh empty state

    sleeper = _FakeSleeper(stop_after=4, on_call=repair)
    monkeypatch.setattr(cli_mod, "_watch_sleep", sleeper)
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--interval", "1"]) == 0

    captured = capsys.readouterr()
    assert sleeper.calls == [2, 4, 1, 1]  # backoff 2x, 4x, then RESET to the plain interval
    assert len(_TICK_FAILED_RE.findall(captured.err)) == 2
    assert "idle: depth=0" in captured.out  # the repaired ticks do their normal work


@requires_git
@pytest.mark.usefixtures("no_watch_traceback")
def test_watch_survives_malformed_repo_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#97 criterion 3: a `repo.yaml` typo is a reported tick failure, not a dead scheduler.

    ``load_repo_config`` is the tick's FIRST statement, so this raise precedes ``worker.run``
    entirely — there is no ``RunReport`` to carry it and the loop must own the rendering.
    """
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    with repo_yaml.open("a", encoding="utf-8") as fh:
        fh.write("\ncron: @daily\n")  # '@' cannot start a YAML token
    sleeper = _FakeSleeper(stop_after=1)
    monkeypatch.setattr(cli_mod, "_watch_sleep", sleeper)
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--interval", "1"]) == 0

    captured = capsys.readouterr()
    lines = [ln for ln in captured.err.splitlines() if " tick failed: " in ln]
    assert len(lines) == 1
    assert _TICK_FAILED_RE.match(lines[0])
    assert "tick failed: ConfigError: malformed YAML in " in lines[0]
    assert str(repo_yaml) in lines[0]  # WHICH file — the operator's next action
    assert "Traceback (most recent call last)" not in captured.err
    assert "agora watch: stopped" in captured.out


@requires_git
@pytest.mark.usefixtures("no_watch_traceback")
def test_watch_recovers_and_resets_backoff_after_repo_yaml_is_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#97 criterion 3 in full: a malformed `repo.yaml` behaves like criteria 1 AND 2.

    The ConfigError path is a DIFFERENT raise site from the ValidationError one — it fires on the
    tick's first statement, before ``recover()`` — so "recovers without a restart and resets the
    backoff" has to be driven here too. Mirrors the criterion-2 schedule exactly: repair during
    sleep 2 ⇒ [2, 4, 1, 1] with two failed ticks.
    """
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    good = repo_yaml.read_text(encoding="utf-8")
    repo_yaml.write_text(f"{good}\ncron: @daily\n", encoding="utf-8")  # '@' starts no YAML token

    def repair(n: int) -> None:
        if n == 1:  # during the 2nd sleep: ticks 1-2 failed, tick 3 will be clean
            repo_yaml.write_text(good, encoding="utf-8")

    sleeper = _FakeSleeper(stop_after=4, on_call=repair)
    monkeypatch.setattr(cli_mod, "_watch_sleep", sleeper)
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--interval", "1"]) == 0

    captured = capsys.readouterr()
    assert sleeper.calls == [2, 4, 1, 1]  # backoff 2x, 4x, then RESET to the plain interval
    assert len(_TICK_FAILED_RE.findall(captured.err)) == 2
    assert "idle: depth=0" in captured.out  # the repaired ticks do their normal work


@requires_git
@pytest.mark.usefixtures("no_watch_traceback")
def test_watch_ctrl_c_still_stops_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#97 criterion 4 (sleep half): Ctrl-C during the sleep exits 0 with the same farewell line."""
    target = tmp_path / "kb"
    _init_stub_repo(target)
    monkeypatch.setattr(cli_mod, "_watch_sleep", _FakeSleeper(stop_after=1))
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--interval", "1"]) == 0

    captured = capsys.readouterr()
    assert captured.out.rstrip().endswith("agora watch: stopped")
    assert "tick failed:" not in captured.err


def test_watch_ctrl_c_from_inside_a_failing_tick_still_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#97 criterion 4, the REAL lock: the new ``except Exception`` must not swallow Ctrl-C.

    ``KeyboardInterrupt`` is a ``BaseException``, so ``except Exception`` provably cannot see it —
    this test is what keeps that property from silently regressing into an unkillable daemon (the
    sleeper trick above only exercises the outer handler, since the sleep sits OUTSIDE the guard).
    """

    def interrupted(repo) -> None:  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "_watch_tick", interrupted)
    monkeypatch.setattr(cli_mod, "_watch_sleep", _fail_if_slept)
    capsys.readouterr()

    assert main(["watch", "--repo", str(tmp_path), "--interval", "1"]) == 0

    captured = capsys.readouterr()
    assert captured.out.rstrip().endswith("agora watch: stopped")
    assert "tick failed:" not in captured.err


def test_watch_backoff_delay_schedule() -> None:
    """#97 criterion 5a: the backoff is a PURE function — schedule, cap, and shift clamp."""
    assert [cli_mod._watch_backoff_delay(60, n) for n in range(7)] == [
        60,
        120,
        240,
        480,
        900,
        900,
        900,
    ]
    assert cli_mod._watch_backoff_delay(60, 0) == 60  # a clean tick keeps the happy path exact
    # The cap is max(interval, 900), never a bare 900: backoff may only ever SLOW the loop down.
    assert cli_mod._watch_backoff_delay(3600, 5) == 3600
    # The shift clamp: without it this allocates a ~10**8-bit integer instead of returning.
    assert cli_mod._watch_backoff_delay(1, 10**9) == 900
    assert all(cli_mod._watch_backoff_delay(1, n) >= 1 for n in range(20))


@pytest.mark.usefixtures("no_watch_traceback")
def test_watch_never_reproduces_the_ten_second_crash_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#97 criterion 5b: a PERMANENTLY failing tick backs off — it never polls faster than
    ``--interval`` and never floods, which is literally "no 10-second crash loop"."""

    def always_raises(repo) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("permanently broken")

    monkeypatch.setattr(cli_mod, "_watch_tick", always_raises)
    sleeper = _FakeSleeper(stop_after=5)
    monkeypatch.setattr(cli_mod, "_watch_sleep", sleeper)
    capsys.readouterr()

    assert main(["watch", "--repo", str(tmp_path), "--interval", "60"]) == 0

    captured = capsys.readouterr()
    assert sleeper.calls == [120, 240, 480, 900, 900]
    assert min(sleeper.calls) >= 60  # never faster than the configured interval
    assert max(sleeper.calls) <= 900  # and bounded, so an operator's fix is seen within 15 min
    assert len(_TICK_FAILED_RE.findall(captured.err)) == 5


@requires_git
def test_watch_backs_off_on_a_malformed_adapters_yaml_that_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#97: the NON-raising dead end must back off too.

    `_build_backend` catches the `adapters.yaml` ConfigError, prints it, and returns None, so this
    tick never reaches the loop's `except Exception`. Before this was fixed the tick counted as a
    success and RESET the backoff — reproduced live at `--interval 1` as 18 multi-line YAML parse
    errors in 8 seconds, forever. That is #97's own disease (one typo amplified into a log flood),
    so it gets #97's own cure: the schedule below is the same 2/4/8/16 curve a raising tick gets.
    """
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("threshold: 10", "threshold: 1"),
        encoding="utf-8",
    )
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )
    # Malformed AFTER the seed, so the tick is genuinely due and reaches _build_backend.
    (target / "adapters.yaml").write_text("backends:\n  qwen: { argv: [x]\n", encoding="utf-8")
    sleeper = _FakeSleeper(stop_after=4)
    monkeypatch.setattr(cli_mod, "_watch_sleep", sleeper)
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--interval", "1"]) == 0

    captured = capsys.readouterr()
    assert sleeper.calls == [2, 4, 8, 16]
    # It is a degraded tick, NOT a raised one: the tick prints its own line and the loop must not
    # relabel it as an exception.
    assert len(_TICK_FAILED_RE.findall(captured.err)) == 0
    assert captured.out.count("but no usable backend") == 4


@requires_git
def test_watch_idle_ticks_do_not_back_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The backoff must not punish the steady state: an empty queue is a correct decision, so
    `idle:` keeps polling at exactly ``--interval`` (the regression guard for the fix above)."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    sleeper = _FakeSleeper(stop_after=3)
    monkeypatch.setattr(cli_mod, "_watch_sleep", sleeper)
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--interval", "5"]) == 0

    assert sleeper.calls == [5, 5, 5]
    assert "idle: depth=0" in capsys.readouterr().out


@requires_git
def test_watch_line_buffers_stdout_so_a_supervisor_sees_the_tick_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`agora watch` must not block-buffer stdout (#96/#97 — reproduced live).

    Python block-buffers stdout whenever it is not a TTY, and BOTH shipped units capture it
    (deploy/systemd/agora-watch.service, deploy/launchd/com.agora.watch.plist). Redirected to a
    file, an 8-second run emitted **0 bytes** of stdout while stderr — line-buffered by default —
    came through: the whole `idle:`/`ran (…)` log AND #96's `failed_record:`/`failed_checks:` pair
    were invisible in exactly the deployment they were written for.
    """
    reconfigured: list[object] = []
    real = sys.stdout.reconfigure

    def spy(**kwargs: object) -> None:
        reconfigured.append(kwargs.get("line_buffering"))
        real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sys.stdout, "reconfigure", spy, raising=False)
    monkeypatch.setattr(cli_mod, "_watch_sleep", _fail_if_slept)
    capsys.readouterr()

    assert main(["watch", "--repo", str(tmp_path), "--once"]) == 0
    assert reconfigured == [True]


@requires_git
@pytest.mark.usefixtures("no_watch_traceback")
def test_watch_once_returns_one_when_the_tick_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#97 criterion 6: ``--once`` still FAILS — same one line, rc 1, and no sleep.

    rc 1 is identical to the pre-guard uncaught-traceback path (the console-script wrapper does not
    catch, so CPython exits 1), so an external scheduler sees exactly what it saw before; what
    changes is that the failure now renders in the SAME grammar as the loop's, keeping one log
    filter for one class of failure.
    """
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    layout.state_file.parent.mkdir(parents=True, exist_ok=True)
    layout.state_file.write_text("not json at all", encoding="utf-8")
    monkeypatch.setattr(cli_mod, "_watch_sleep", _fail_if_slept)
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 1

    captured = capsys.readouterr()
    assert len(_TICK_FAILED_RE.findall(captured.err)) == 1
    assert "tick failed: ValidationError: " in captured.err
    assert "Traceback (most recent call last)" not in captured.err


@requires_git
def test_watch_startup_banner_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#97 criterion 7: both banner forms are byte-identical — the guard added no output."""
    target = tmp_path / "kb"
    _init_stub_repo(target)
    root = Repo.resolve(str(target)).layout.root
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == f"agora watch: {root} [once]"

    monkeypatch.setattr(cli_mod, "_watch_sleep", _FakeSleeper(stop_after=1))
    assert main(["watch", "--repo", str(target), "--interval", "60"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == f"agora watch: {root} (interval=60s)"


@requires_git
def test_watch_traceback_env_prints_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``AGORA_WATCH_TRACEBACK`` ADDS a traceback for bug reports — it never REPLACES the line."""
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    layout.state_file.parent.mkdir(parents=True, exist_ok=True)
    layout.state_file.write_text("not json at all", encoding="utf-8")
    monkeypatch.setenv("AGORA_WATCH_TRACEBACK", "1")
    monkeypatch.setattr(cli_mod, "_watch_sleep", _fail_if_slept)
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 1

    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert len(_TICK_FAILED_RE.findall(err)) == 1  # the one-line summary is still there


@requires_git
@pytest.mark.parametrize("value", ["0", "false", "no", "off", " "])
def test_watch_traceback_env_falsey_values_are_off(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Truthiness is EXPLICIT: ``AGORA_WATCH_TRACEBACK=0`` meaning "on" would be a footgun."""
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    layout.state_file.parent.mkdir(parents=True, exist_ok=True)
    layout.state_file.write_text("not json at all", encoding="utf-8")
    monkeypatch.setenv("AGORA_WATCH_TRACEBACK", value)
    monkeypatch.setattr(cli_mod, "_watch_sleep", _fail_if_slept)
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 1

    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" not in err
    assert len(_TICK_FAILED_RE.findall(err)) == 1


def test_tick_failure_detail_is_one_bounded_line() -> None:
    """The tick detail is ONE line and BOUNDED — an unbounded exception must not flood a journal."""
    long_line = cli_mod._tick_failure_detail(RuntimeError("X" * 5000))
    assert "\n" not in long_line
    assert len(long_line) <= cli_mod._TICK_DETAIL_CHARS + len("RuntimeError: ") + 1
    assert long_line.endswith("…")
    # Whitespace-COLLAPSED, not first-line-truncated: the diagnosis is routinely on line 2.
    assert cli_mod._tick_failure_detail(RuntimeError("a\nb")) == "RuntimeError: a b"
    assert cli_mod._tick_failure_detail(RuntimeError()) == "RuntimeError: <no message>"


# --- import (the vault normalizer, ADR-0014 D5 — now emitting KB wiki schema 2) ------------------
@requires_git
def test_import_happy_path_creates_dest_and_prints_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`agora import` on a tiny vault exits 0, git-inits dest, and prints the imported count."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    note = src / "wiki" / "general" / "themes" / "topic.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Topic\n\nA body paragraph about the topic.\n", encoding="utf-8")

    rc = main(["import", str(src), str(dest)])

    assert rc == 0  # a best-effort import is a success even with lint findings
    out = capsys.readouterr().out
    assert "imported 1 note(s)" in out
    # dest is a real, git-inited Agora repo with the schema emitted.
    assert (dest / ".git").exists()
    assert (dest / "AGENTS.md").is_file()


@requires_git
def test_import_defaults_to_general_domain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With --domain omitted, an off-layout note lands in the schema-2 CONCEPT directory.

    The v1 assertion was ``wiki/general/themes/loose-idea.md`` — domain-first, ``themes`` naming
    the kind. Under ADR-0041 the directory IS the kind and the domain has left the path for
    ``subjects:``, so the same note lands in ``wiki/concepts/`` and the default domain survives as
    a subject rather than as a folder. The digest also names the navigation tier the importer
    SYNTHESIZES (D5), which has no counterpart in the source vault.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    note = src / "Loose Idea.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Loose Idea\n\nA stray note.\n", encoding="utf-8")

    rc = main(["import", str(src), str(dest)])

    assert rc == 0
    assert "synthesized_maps=" in capsys.readouterr().out
    imported = dest / "wiki" / "concepts" / "loose-idea.md"
    assert imported.is_file()
    assert not (dest / "wiki" / "general").exists()
    assert "kind: concept" in imported.read_text(encoding="utf-8")


@requires_git
def test_import_missing_src_exits_1_with_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing source vault is a HARD error: exit 1 + an error on stderr (ADR-0014 D5)."""
    dest = tmp_path / "out"

    rc = main(["import", str(tmp_path / "nope"), str(dest)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "import:" in err


@requires_git
def test_import_with_warnings_still_exits_0_and_prints_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A vault with findings (off-layout move + stripped tag) exits 0 and prints the warnings."""
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    note = src / "Loose Idea.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    # Off-layout (forces a move warning) + a tag outside the declared taxonomy (forces a strip).
    note.write_text(
        "---\ntitle: Loose\ntags: [unknown-tag]\n---\n\n# Loose Idea\n\nA stray note.\n",
        encoding="utf-8",
    )

    rc = main(["import", str(src), str(dest), "--tag", "architecture"])

    assert rc == 0  # warnings are NOT a failure
    out = capsys.readouterr().out
    assert "moved to fit" in out
    assert "stripped tags: unknown-tag" in out


@requires_git
def test_import_produces_a_repo_this_build_can_actually_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The inverse of the note this replaces: an imported repo is CURATE-ABLE the moment it exists.

    While the importer emitted schema 1, ``agora import`` minted a repo that was read-only the
    moment it was created and had to say so out loud — a ``lint: clean`` line over a repo the
    operator cannot curate is a true statement that leaves a false impression. The importer now
    emits schema 2 (ADR-0041 D6), so the warning must be GONE and its absence must be earned: the
    proof is that the write path, which refuses a schema-1 repo, accepts this one.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "out"
    note = src / "wiki" / "general" / "themes" / "topic.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Topic\n\nA body paragraph.\n", encoding="utf-8")

    rc = main(["import", str(src), str(dest)])

    assert rc == 0
    combined = capsys.readouterr()
    assert "READ but not WRITE" not in combined.err
    assert "READ-ONLY" not in combined.err + combined.out
    # The claim is TRUE, not merely unstated: the ADR-0041 D6 write refusal does NOT fire here.
    Inbox(RepoLayout(dest)).write(text="A captured fact.", writer="test", source="manual")
    assert main(["status", "--repo", str(dest)]) == 0
    assert "READ-ONLY" not in capsys.readouterr().out


@requires_git
def test_import_into_a_schema1_repo_exits_1_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two layouts in one repo is the state D6's refusal exists to prevent.

    The guard flipped with the importer: it now emits schema 2, so the destination it must refuse
    is a schema-**1** repo (importing there would commit ``wiki/concepts/…`` beside
    ``wiki/<domain>/themes/…``). The remedy on the message is the one crossing that exists.
    """
    src = tmp_path / "vault"
    dest = tmp_path / "kb1"
    note = src / "wiki" / "general" / "themes" / "topic.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Topic\n\nA body paragraph.\n", encoding="utf-8")
    assert main(["repo", "init", str(dest), "--schema", "1", "--domain", "general"]) == 0
    capsys.readouterr()

    rc = main(["import", str(src), str(dest)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "schema-1 KB" in err
    assert "import --from-kb" in err
    assert not (dest / "wiki" / "concepts").exists()


# --- import --from-kb (the schema-1 → schema-2 CONVERTER, ADR-0041 D6) ---------------------------
def _schema_1_kb(root: Path) -> Path:
    """A small but SHAPE-COMPLETE schema-1 KB: a map per domain, two themes, two same-date dailies.

    Every D6 rule that produces visible output is exercised by this one corpus: the ``-moc`` rename
    (rule 3), the two dailies of DIFFERENT domains sharing a date (rule 4's merge), the path domain
    that must survive as a subject (rule 2) and the ``raw/`` the builder materializes for each
    concept's ``sources:`` (rule 5).
    """
    return build_kb(
        root,
        [
            NoteSpec(
                kind="theme",
                domain="ai-tech",
                title="Retrieval Augmented Generation",
                body="Retrieval augmented generation grounds an answer in retrieved documents.",
            ),
            NoteSpec(
                kind="theme",
                domain="ml",
                title="Gradient Descent",
                body="Gradient descent walks a loss surface downhill one step at a time.",
            ),
            NoteSpec(
                kind="daily",
                domain="ai-tech",
                title="ai-tech 2026-01-12",
                body="Captured one note about retrieval.",
                extra_frontmatter={"date": "2026-01-12"},
            ),
            NoteSpec(
                kind="daily",
                domain="ml",
                title="ml 2026-01-12",
                body="Captured one note about gradients.",
                extra_frontmatter={"date": "2026-01-12"},
            ),
        ],
        schema_version=1,
        domains=["ai-tech", "ml"],
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` by repo-relative path → bytes. The "src is never touched" proof."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@requires_git
def test_import_from_kb_converts_a_schema_1_repo_and_prints_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The D6 crossing, end to end: a schema-1 repo in, a WRITABLE schema-2 repo out.

    Four claims, and the last one is the point of the whole wave: the destination has the
    kind-first shape, the report enumerates the renames and the merge (a converter that renames
    silently is one that loses ``[[basename]]`` edges), the SOURCE is byte-for-byte untouched, and
    the destination accepts the capture that the source refuses.
    """
    src = _schema_1_kb(tmp_path / "v1")
    dest = tmp_path / "v2"
    before = _snapshot(src)

    rc = main(["import", "--from-kb", str(src), str(dest)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "converted 6 note(s)" in out  # 7 v1 notes in, two dailies merged into one journal
    assert "source_notes=7" in out
    assert "kb_id: " in out
    # The COUNT and the LIST are different quantities (a merged journal is one destination note but
    # two basename pairs), so they are labelled apart rather than both printed as "renamed".
    assert "renamed_notes=" in out
    assert "renamed basenames (" in out
    assert "ai-tech-moc -> ai-tech" in out
    assert "merges (1):" in out
    assert "2026-01-12 <- " in out
    assert "lint: clean" in out
    # The kind-first shape: the map lost its `-moc` suffix, the concepts left their domain folder,
    # and the two same-date dailies became ONE journal sharded under <yyyy>/<mm>.
    assert (dest / "wiki" / "maps" / "ai-tech.md").is_file()
    assert (dest / "wiki" / "concepts" / "retrieval-augmented-generation.md").is_file()
    assert (dest / "wiki" / "notes" / "2026" / "01" / "2026-01-12.md").is_file()
    assert not (dest / "wiki" / "ai-tech").exists()
    assert (dest / "_meta" / "kb.yaml").is_file()
    # The source is untouched — not "still lints", but the same bytes in the same files.
    assert _snapshot(src) == before
    # ...and the destination is the half this build can write, while the source still is not.
    with pytest.raises(ReadOnlySchemaVersionError):
        Inbox(RepoLayout(src)).write(text="A captured fact.", writer="test", source="manual")
    Inbox(RepoLayout(dest)).write(text="A captured fact.", writer="test", source="manual")
    assert main(["status", "--repo", str(dest)]) == 0
    assert "READ-ONLY" not in capsys.readouterr().out


@requires_git
def test_import_from_kb_refuses_the_vault_import_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--domain`` / ``--tag`` are the VAULT path's flags and are refused, never ignored.

    The converter carries the source repo's taxonomy across unchanged, so accepting ``--domain``
    silently would let an operator believe they had re-shaped a taxonomy that in fact came over as
    it was. Exit 2 (the argparse usage code) and NOTHING written.
    """
    src = _schema_1_kb(tmp_path / "v1")
    dest = tmp_path / "v2"

    rc = main(["import", "--from-kb", str(src), str(dest), "--domain", "other"])

    assert rc == 2
    assert "--domain/--tag are vault-import flags" in capsys.readouterr().err
    assert not dest.exists()


@requires_git
def test_import_from_kb_refuses_a_destination_inside_the_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``agora import --from-kb <kb> <kb>/converted`` is refused, and the source is untouched.

    The CLI closes every successful conversion with ``<src> was NOT modified``. A destination
    nested in the source made that line false — the run planted a whole schema-2 repo, its own
    ``.git`` included, inside the tree it had just read — so the pairing is refused outright rather
    than the note being softened into a maybe.
    """
    src = _schema_1_kb(tmp_path / "v1")
    before = _snapshot(src)

    rc = main(["import", "--from-kb", str(src), str(src / "converted")])

    assert rc == 1
    assert "inside the source repo" in capsys.readouterr().err
    assert not (src / "converted").exists()
    assert _snapshot(src) == before


@requires_git
def test_import_into_an_existing_repo_is_refused_naming_the_inbox(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The VAULT lane writes a NEW repo too: an already-initialized destination is refused.

    Before the guard, a second ``agora import`` into a live schema-2 KB exited 0 and printed
    ``lint: clean`` while re-minting ``_meta/kb.yaml`` with a fresh ``kb_id`` the existing notes did
    not carry (D1.5 mints one ONCE) and rebuilding the root map from that vault alone.
    """
    dest = tmp_path / "kb"
    assert main(["repo", "init", str(dest), "--domain", "general"]) == 0
    before = _snapshot(dest)
    vault = tmp_path / "vault"
    (vault / "general").mkdir(parents=True)
    (vault / "general" / "alpha.md").write_text("# Alpha\n\nProse.\n", encoding="utf-8")

    rc = main(["import", str(vault), str(dest), "--domain", "general"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "already a schema-2 KB" in err
    assert "kb_remember" in err  # ...and it names the way to ADD to an existing KB
    assert _snapshot(dest) == before


@requires_git
def test_import_from_kb_refuses_a_source_that_is_not_schema_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """There is exactly ONE crossing and it runs in exactly one direction (ADR-0041 D6)."""
    src = tmp_path / "kb2"
    assert main(["repo", "init", str(src), "--domain", "general"]) == 0
    capsys.readouterr()

    rc = main(["import", "--from-kb", str(src), str(tmp_path / "out")])

    assert rc == 1
    err = capsys.readouterr().err
    assert "is not a KB wiki schema-1 repo" in err
    assert "Traceback" not in err


@requires_git
def test_import_from_kb_refuses_an_occupied_destination(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Converting INTO a populated tree is the two-layouts-in-one-repo state D6 prevents."""
    src = _schema_1_kb(tmp_path / "v1")
    dest = tmp_path / "v2"
    dest.mkdir()
    (dest / "occupied.md").write_text("mine\n", encoding="utf-8")

    rc = main(["import", "--from-kb", str(src), str(dest)])

    assert rc == 1
    assert "never converts in place" in capsys.readouterr().err
    assert (dest / "occupied.md").read_text(encoding="utf-8") == "mine\n"


@requires_git
def test_import_from_kb_names_every_colliding_basename_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D6 rule 7: a collision the flip INTRODUCES is a hard failure with a named list.

    Two v1 concepts of different domains may share a basename — the domain was in the path. Under
    schema 2 both want ``wiki/concepts/<slug>.md``, and a converter that silently renamed one would
    break every ``[[basename]]`` edge pointing at it. Exit 1, the names printed, dest untouched.
    """
    src = _schema_1_kb(tmp_path / "v1")
    twin = src / "wiki" / "ml" / "themes" / "retrieval-augmented-generation.md"
    twin.write_bytes(
        (src / "wiki" / "ai-tech" / "themes" / "retrieval-augmented-generation.md").read_bytes()
    )
    dest = tmp_path / "v2"

    rc = main(["import", "--from-kb", str(src), str(dest)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "retrieval-augmented-generation" in err
    assert "Traceback" not in err
    assert not dest.exists()


@requires_git
def test_import_from_kb_missing_src_exits_1_with_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing source is a HARD error on this path too — exit 1, one line, no traceback."""
    rc = main(["import", "--from-kb", str(tmp_path / "nope"), str(tmp_path / "out")])

    assert rc == 1
    err = capsys.readouterr().err
    assert "import --from-kb:" in err
    assert "Traceback" not in err


def test_import_help_names_the_schema_it_writes_and_the_one_crossing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``agora import --help`` must answer "which layout do I get?" without reading an ADR.

    Whitespace-normalized, and every asserted phrase is HYPHEN-FREE: argparse re-wraps help to the
    terminal width and ``textwrap`` will break a word at a hyphen, so "no in-place migrator" is not
    a substring of its own rendered help at 80 columns.
    """
    with pytest.raises(SystemExit):
        main(["import", "--help"])

    out = " ".join(capsys.readouterr().out.split())
    assert "KB wiki schema 2" in out
    assert "--from-kb" in out
    assert "the only crossing between the two schemas" in out


def test_top_level_help_lists_import_as_a_schema_2_producer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The subcommand table is where an operator first meets ``import``; it must not be neutral."""
    with pytest.raises(SystemExit):
        main(["--help"])

    assert "new KB schema-2 Agora repo" in " ".join(capsys.readouterr().out.split())


# --- --version (issue #101) ----------------------------------------------------------------------
def test_version_flag_prints_the_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#101 criterion 1: ``agora --version`` → ``agora <ver>`` on stdout, exit 0, nothing else.

    Three properties, each load-bearing. **No subcommand**: this is the first thing a confused user
    types, so the flag lives on the top-level parser (before #101 it exited 2 as an unknown
    argument). **Exit 0**: a version query is a success, and packaging/CI smoke checks branch on the
    code. **Stdout, alone**: the line gets piped and pasted, so a stderr byte or a stray banner
    would corrupt it.

    The expected string is built from :data:`agora_kb.__version__` rather than the literal
    ``0.1.0b1`` deliberately — hardcoding it here would create the SECOND copy of the version that
    this issue exists to eliminate. The literal's own shape is locked in ``tests/test_version.py``.
    """
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])

    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == f"agora {__version__}\n"
    assert captured.err == ""


def test_version_flag_is_not_shadowed_by_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    """The flag must be resolved by the top-level parser, i.e. accepted BEFORE any subcommand.

    argparse gives a subparser its own namespace, so a `--version` placed after a subcommand name
    would belong to that subcommand (and fail). Asserting the pre-subcommand position is what
    documents the supported form; it also pins that adding subcommands later cannot silently
    capture the flag.
    """
    with pytest.raises(SystemExit) as excinfo:
        main(["--version", "status"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out == f"agora {__version__}\n"


# --- no / unknown command -----------------------------------------------------------------------
def test_no_command_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_unknown_command_exits_2() -> None:
    # argparse rejects an unknown subcommand by raising SystemExit(2) during parsing.
    with pytest.raises(SystemExit) as excinfo:
        main(["definitely-not-a-command"])
    assert excinfo.value.code == 2


# --- harvest (ADR-0007/0017) --------------------------------------------------------------------
def _setup_harvest_repo(
    tmp_path: Path,
    *,
    kind: str = "personal",
    scope: str = "personal",
    enabled: bool = True,
    with_connector: bool = True,
) -> tuple[Path, Path]:
    """Init a repo, enable harvest in repo.yaml, and (optionally) wire a file: connector."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target), "--kind", kind, "--domain", "general"]) == 0
    mem = tmp_path / "mem" / "MEMORY.md"
    mem.parent.mkdir(parents=True, exist_ok=True)
    mem.write_text("# m\n\n- harvested fact one\n- harvested fact two\n", encoding="utf-8")

    rp = target / "_kb" / "repo.yaml"
    doc = yaml.safe_load(rp.read_text(encoding="utf-8"))
    doc.setdefault("harvest", {})["enabled"] = enabled
    rp.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    if with_connector:
        ap = target / "adapters.yaml"
        a = yaml.safe_load(ap.read_text(encoding="utf-8"))
        a["connectors"] = {"file:demo": {"path": str(mem), "scope": scope}}
        ap.write_text(yaml.safe_dump(a, sort_keys=False), encoding="utf-8")
    return target, mem


@requires_git
def test_harvest_disabled_is_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, _ = _setup_harvest_repo(tmp_path, enabled=False)
    capsys.readouterr()
    rc = main(["harvest", "--repo", str(target)])
    assert rc == 0
    assert "disabled" in capsys.readouterr().out


@requires_git
def test_harvest_writes_gated_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, _ = _setup_harvest_repo(tmp_path)
    capsys.readouterr()
    rc = main(["harvest", "--repo", str(target)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "total candidates written: 2" in out
    items = sorted((target / "_kb" / "inbox").glob("*/*.md"))
    assert len(items) == 2
    for p in items:
        text = p.read_text(encoding="utf-8")
        assert "kind: candidate" in text
        assert "confidence: low" in text
        assert "source: harvest:demo" in text


@requires_git
def test_harvest_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, _ = _setup_harvest_repo(tmp_path)
    capsys.readouterr()
    rc = main(["harvest", "--repo", str(target), "--dry-run"])
    assert rc == 0
    assert "would harvest" in capsys.readouterr().out
    assert list((target / "_kb" / "inbox").glob("*/*.md")) == []


@requires_git
def test_harvest_no_connectors_is_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, _ = _setup_harvest_repo(tmp_path, with_connector=False)
    capsys.readouterr()
    rc = main(["harvest", "--repo", str(target)])
    assert rc == 1
    assert "no connectors configured" in capsys.readouterr().err


@requires_git
def test_harvest_unknown_connector_is_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, _ = _setup_harvest_repo(tmp_path)
    capsys.readouterr()
    rc = main(["harvest", "--repo", str(target), "--connector", "file:bogus"])
    assert rc == 1
    assert "no connector named" in capsys.readouterr().out


@requires_git
def test_harvest_scope_refused_exits_zero_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A personal source into a team repo is refused (privacy); the run still completes (exit 0).
    target, _ = _setup_harvest_repo(tmp_path, kind="team", scope="personal")
    capsys.readouterr()
    rc = main(["harvest", "--repo", str(target)])
    assert rc == 0
    assert "SCOPE REFUSED" in capsys.readouterr().out
    assert list((target / "_kb" / "inbox").glob("*/*.md")) == []


@requires_git
def test_harvest_malformed_adapters_is_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, _ = _setup_harvest_repo(tmp_path)
    ap = target / "adapters.yaml"
    a = yaml.safe_load(ap.read_text(encoding="utf-8"))
    a["connectors"] = {"file:demo": {"path": "/tmp/x/MEMORY.md", "scope": "bogus-scope"}}
    ap.write_text(yaml.safe_dump(a, sort_keys=False), encoding="utf-8")
    capsys.readouterr()
    rc = main(["harvest", "--repo", str(target)])
    assert rc == 1
    assert "invalid config" in capsys.readouterr().err


@requires_git
def test_doctor_prints_the_connectors_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, _ = _setup_harvest_repo(tmp_path)
    capsys.readouterr()
    main(["doctor", "--repo", str(target), "--skip-probe"])
    out = capsys.readouterr().out
    assert "harvest: enabled (scope_lock=personal)" in out
    assert "file:demo (scope=personal)" in out
    assert "proposed=0" in out


@requires_git
def test_doctor_reports_the_session_connector_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`agora doctor` answers "which grammar is reading my transcripts" (issue #147).

    An undeclared format prints the DEFAULT it resolves to, not a blank — an operator debugging a
    zero-fact harvest needs the EFFECTIVE parser. The file: line stays exactly as it was: format is
    a session-only concept and a column of `format=n/a` would be noise.
    """
    target, _ = _setup_harvest_repo(tmp_path)
    ap = target / "adapters.yaml"
    a = yaml.safe_load(ap.read_text(encoding="utf-8"))
    a["connectors"]["session:demo"] = {"path": str(tmp_path / "s" / "*.jsonl"), "scope": "personal"}
    a["connectors"]["session:pinned"] = {
        "path": str(tmp_path / "s" / "*.jsonl"),
        "scope": "personal",
        "format": "claude-code-jsonl",
    }
    ap.write_text(yaml.safe_dump(a, sort_keys=False), encoding="utf-8")
    capsys.readouterr()
    main(["doctor", "--repo", str(target), "--skip-probe"])
    out = capsys.readouterr().out
    assert "session:demo (scope=personal, format=claude-code-jsonl)" in out
    assert "session:pinned (scope=personal, format=claude-code-jsonl)" in out
    assert "file:demo (scope=personal)" in out  # unchanged: no format column on a file: connector


@requires_git
def test_a_targeted_harvest_is_not_hostage_to_another_connector(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--connector NAME` narrows BEFORE `build_connectors`, which is all-or-nothing (#147 review).

    `build_connectors` raises on the FIRST spec it cannot construct, so without the pre-filter one
    mis-declared connector disabled harvesting for every healthy one — including a run that named
    a different connector explicitly.
    """
    target, _ = _setup_harvest_repo(tmp_path)
    ap = target / "adapters.yaml"
    a = yaml.safe_load(ap.read_text(encoding="utf-8"))
    # A connector TYPE the config loader accepts and `build_connectors` refuses (no such family).
    a["connectors"]["mail:nope"] = {"path": str(tmp_path / "m"), "scope": "personal"}
    ap.write_text(yaml.safe_dump(a, sort_keys=False), encoding="utf-8")

    capsys.readouterr()
    assert main(["harvest", "--repo", str(target), "--connector", "file:demo", "--dry-run"]) == 0
    assert "would harvest" in capsys.readouterr().out

    # Un-targeted, the bad connector is still a loud, clean failure — nothing is swallowed.
    assert main(["harvest", "--repo", str(target), "--dry-run"]) == 1
    assert "mail:nope" in capsys.readouterr().err


@requires_git
def test_harvest_unknown_connector_still_lists_the_configured_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The "no connector named X" message names the whole configured set, not the filtered one."""
    target, _ = _setup_harvest_repo(tmp_path)
    capsys.readouterr()
    assert main(["harvest", "--repo", str(target), "--connector", "file:bogus"]) == 1
    assert "['file:demo']" in capsys.readouterr().out


@requires_git
def test_harvest_follow_links_harvests_sibling_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, mem = _setup_harvest_repo(tmp_path)
    # Reshape the memory as a pointer index + a same-dir sibling, and turn follow_links on.
    mem.write_text("# Index\n\n- [Curator](curator.md) — how it works\n", encoding="utf-8")
    (mem.parent / "curator.md").write_text(
        "# Curator\n\nOne curator holds a per-repo lock.\n", encoding="utf-8"
    )
    ap = target / "adapters.yaml"
    a = yaml.safe_load(ap.read_text(encoding="utf-8"))
    a["connectors"]["file:demo"]["follow_links"] = True
    ap.write_text(yaml.safe_dump(a, sort_keys=False), encoding="utf-8")
    capsys.readouterr()

    rc = main(["harvest", "--repo", str(target)])
    assert rc == 0
    items = sorted((target / "_kb" / "inbox").glob("*/*.md"))
    assert len(items) == 1
    body = items[0].read_text(encoding="utf-8")
    assert "One curator holds a per-repo lock." in body  # the SIBLING content was harvested
    assert "[Curator](curator.md)" not in body  # the thin pointer markup was replaced


# --- gold packs (ADR-0027, issue #37) -----------------------------------------------------------
def _gold_repo(tmp_path: Path) -> Path:
    """Init a repo, add one eligible theme note, and commit it (hermetic git env)."""
    import os
    import subprocess

    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    themes = target / "wiki" / "concepts"
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "curator-concurrency.md").write_text(
        "---\ntitle: Curator Concurrency\ntype: theme\naliases: []\ntags: []\n"
        "created: '2026-06-01'\nupdated: '2026-07-01'\nstatus: active\n"
        "summary: single-writer CAS keeps the wiki consistent\nsources: [raw/a.md]\n"
        "related: []\nconfidence: high\n---\n\n# Curator Concurrency\n\nbody\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_DATE": "2026-07-05T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-07-05T00:00:00+00:00",
    }
    subprocess.run(["git", "add", "-A"], cwd=target, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "-m", "theme"], cwd=target, check=True, capture_output=True, env=env
    )
    return target


@requires_git
def test_cli_gold_build_status_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = _gold_repo(tmp_path)
    root = str(target)

    assert main(["gold", "build", "--repo", root]) == 0
    out = capsys.readouterr().out
    assert "built pack 'default'" in out
    assert RepoLayout(target).gold_pack_path("default").is_file()

    assert main(["gold", "status", "--repo", root]) == 0
    out = capsys.readouterr().out
    assert "FRESH" in out and "Curator Concurrency" not in out  # status is a meta read, not content

    # --check on a fresh pack passes (byte-identical rebuild contract).
    assert main(["gold", "build", "--repo", root, "--check"]) == 0
    assert "byte-identical" in capsys.readouterr().out


@requires_git
def test_cli_gold_check_detects_stale(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = _gold_repo(tmp_path)
    root = str(target)
    assert main(["gold", "build", "--repo", root]) == 0
    capsys.readouterr()
    # Corrupt the on-disk pack: --check must fail (exit 1) against a fresh rebuild.
    RepoLayout(target).gold_pack_path("default").write_text("tampered\n", encoding="utf-8")
    assert main(["gold", "build", "--repo", root, "--check"]) == 1
    assert "DIFFERS" in capsys.readouterr().out


@requires_git
def test_cli_gold_status_absent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = _gold_repo(tmp_path)
    assert main(["gold", "status", "--repo", str(target)]) == 0
    assert "absent" in capsys.readouterr().out


def test_cli_gold_missing_subcommand_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gold"]) == 2
    assert "usage: agora gold" in capsys.readouterr().out


@requires_git
def test_cli_doctor_reports_gold_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = _gold_repo(tmp_path)
    assert main(["gold", "build", "--repo", str(target)]) == 0
    capsys.readouterr()
    main(["doctor", "--repo", str(target), "--skip-probe"])
    out = capsys.readouterr().out
    assert "gold: pack=fresh" in out and "_kb/gold/" in out


# --- sync + auto backup (push-only git backup, issue #64) ----------------------------------------
def _set_backup(layout: RepoLayout, *, remote: str, auto: bool = False) -> None:
    """Add a backup: block to an existing _kb/repo.yaml (the issue-#64 opt-in)."""
    repo_yaml = layout.kb_dir / "repo.yaml"
    doc = yaml.safe_load(repo_yaml.read_text(encoding="utf-8"))
    doc["backup"] = {"remote": remote, "auto": auto}
    repo_yaml.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _cli_bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True
    )
    return remote


def _rev_parse(git_dir: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=str(git_dir), capture_output=True, text=True, check=True
    ).stdout.strip()


@requires_git
def test_sync_without_remote_is_a_guided_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(a) No backup.remote configured → `agora sync` explains how to enable it and exits 0
    (a no-op success, NOT an error — safe to script unconditionally)."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    capsys.readouterr()

    assert main(["sync", "--repo", str(target)]) == 0

    captured = capsys.readouterr()
    assert "no backup remote configured" in captured.out
    assert "backup.remote" in captured.out
    assert captured.err == ""
    assert not (RepoLayout(target).kb_dir / "backup.json").exists()  # nothing recorded


@requires_git
def test_sync_pushes_to_local_bare_remote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(b) A configured remote → `agora sync` really pushes: the bare remote's main reaches the
    local curated tip, and the outcome lands in _kb/backup.json for doctor."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    layout = RepoLayout(target)
    remote = _cli_bare_remote(tmp_path)
    _set_backup(layout, remote=str(remote))
    capsys.readouterr()

    assert main(["sync", "--repo", str(target)]) == 0

    out = capsys.readouterr().out
    assert "sync: pushed main @" in out
    assert _rev_parse(remote, "refs/heads/main") == _rev_parse(target, "HEAD")
    state = json.loads((layout.kb_dir / "backup.json").read_text(encoding="utf-8"))
    assert state["ok"] is True
    assert state["remote"] == str(remote)


@requires_git
def test_sync_push_failure_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(c) An unreachable remote → exit 1 with a clean stderr message (no traceback), and the
    failure is recorded for the doctor line."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    layout = RepoLayout(target)
    _set_backup(layout, remote=str(tmp_path / "missing-remote.git"))
    capsys.readouterr()

    assert main(["sync", "--repo", str(target)]) == 1

    err = capsys.readouterr().err
    assert "sync: push failed" in err
    state = json.loads((layout.kb_dir / "backup.json").read_text(encoding="utf-8"))
    assert state["ok"] is False

    # doctor renders the recorded failure as ONE compressed line (first error line only — a
    # multi-line git stderr must never flood the health report; the full text stays in the file).
    main(["doctor", "--repo", str(target), "--skip-probe"])
    out = capsys.readouterr().out
    backup_lines = [ln for ln in out.splitlines() if ln.lstrip().startswith("backup: remote=")]
    assert len(backup_lines) == 1
    assert "FAILED (" in backup_lines[0]


@requires_git
def test_sync_on_an_uninitialized_repo_path_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typoed/unmounted --repo path exits 1 BEFORE the guided no-op: an unreadable repo also
    reads as "no remote configured", so checking remote first would make a cron'd sync report
    success forever while never pushing — the silent-not-backing-up failure mode the backup
    config parsing itself refuses."""
    missing = tmp_path / "no-such-kb"

    assert main(["sync", "--repo", str(missing)]) == 1

    captured = capsys.readouterr()
    assert "not initialized" in captured.err
    assert "no backup remote configured" not in captured.out


@requires_git
def test_watch_auto_backup_pushes_after_published_curation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """backup.auto=true → a watch tick that PUBLISHES pushes best-effort afterwards; the remote
    ends at the published curated tip."""
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("threshold: 10", "threshold: 1"),
        encoding="utf-8",
    )
    remote = _cli_bare_remote(tmp_path)
    _set_backup(layout, remote=str(remote), auto=True)
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    out = capsys.readouterr().out
    assert "ran (threshold)" in out and "status=published" in out
    assert "backup pushed: main @" in out
    assert _rev_parse(remote, "refs/heads/main") == _rev_parse(target, "refs/heads/main")


@requires_git
def test_watch_auto_backup_failure_never_fails_the_curation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(c) auto=true + an unreachable remote → the tick still publishes and exits 0; the push
    failure is one best-effort warning line, not a curation failure."""
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("threshold: 10", "threshold: 1"),
        encoding="utf-8",
    )
    _set_backup(layout, remote=str(tmp_path / "missing-remote.git"), auto=True)
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    out = capsys.readouterr().out
    assert "status=published" in out  # the curation itself succeeded
    assert "backup push failed (best-effort; curation unaffected)" in out


@requires_git
def test_watch_auto_backup_skips_a_tick_that_did_not_publish(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The auto push fires ONLY on a published tick: a run that FAILS (brain crash) with
    backup.auto=true and a live remote produces ZERO backup side effects — no push output, no
    _kb/backup.json, no branch on the remote. Locks the `status == "published"` gate (a
    regression to push-on-every-ran-tick would pass every other watch test)."""
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("threshold: 10", "threshold: 1"),
        encoding="utf-8",
    )
    remote = _cli_bare_remote(tmp_path)
    _set_backup(layout, remote=str(remote), auto=True)
    # Break the brain AFTER init: the tick still runs, but the consolidation FAILS.
    (target / "stub_brain.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    out = capsys.readouterr().out
    assert "status=failed" in out  # the tick ran and did NOT publish
    assert "backup pushed" not in out
    assert "backup push failed" not in out
    assert not (layout.kb_dir / "backup.json").exists()
    probe = subprocess.run(  # the bare remote never received the branch
        ["git", "rev-parse", "--verify", "refs/heads/main"],
        cwd=str(remote),
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0


@requires_git
def test_watch_auto_push_is_non_interactive_and_time_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The watch tick's auto push must never stall the scheduler: it calls push_backup with
    interactive=False (credential/host-key prompts fail fast) and a FINITE timeout — locks the
    call-site wiring of the unattended-push posture."""
    import agora_kb.cli as cli_mod

    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("threshold: 10", "threshold: 1"),
        encoding="utf-8",
    )
    _set_backup(layout, remote="git@example.com:me/kb.git", auto=True)
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )
    seen: dict[str, object] = {}

    def push_spy(self, remote, *, branch=None, timeout=None, interactive=True):  # type: ignore[no-untyped-def]
        seen.update(remote=remote, timeout=timeout, interactive=interactive)
        return "f" * 40

    monkeypatch.setattr(cli_mod.Repo, "push_backup", push_spy)
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    assert "status=published" in capsys.readouterr().out
    assert seen["interactive"] is False
    assert isinstance(seen["timeout"], float)
    assert seen["timeout"] <= 300.0


@requires_git
def test_watch_without_backup_config_stays_silent_about_backup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(a) With NO backup: block (auto defaults off), a publishing watch tick emits no backup
    output and records no backup state — the pre-#64 path is undisturbed."""
    target = tmp_path / "kb"
    layout = _init_stub_repo(target)
    repo_yaml = layout.kb_dir / "repo.yaml"
    repo_yaml.write_text(
        repo_yaml.read_text(encoding="utf-8").replace("threshold: 10", "threshold: 1"),
        encoding="utf-8",
    )
    Inbox(layout).write(
        text="One curator advances the branch under a lock.",
        writer="dochan",
        source="claude-code",
        domain="ai-tech",
        now=datetime(2026, 6, 13, 2, 40, 10, tzinfo=UTC),
    )
    capsys.readouterr()

    assert main(["watch", "--repo", str(target), "--once"]) == 0

    out = capsys.readouterr().out
    assert "status=published" in out
    # None of the #64 backup markers appear (the tmp_path itself contains "backup", so match the
    # actual output lines, not the bare word).
    assert "backup pushed" not in out
    assert "backup push failed" not in out
    assert "backup config invalid" not in out
    assert not (layout.kb_dir / "backup.json").exists()


@requires_git
def test_doctor_prints_the_backup_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`agora doctor` reports backup observability: unconfigured → a clear off note; after a real
    sync → remote + auto + the last recorded push (never affecting the health verdict)."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    capsys.readouterr()
    main(["doctor", "--repo", str(target), "--skip-probe"])
    assert "backup: no remote configured" in capsys.readouterr().out

    layout = RepoLayout(target)
    remote = _cli_bare_remote(tmp_path)
    _set_backup(layout, remote=str(remote), auto=True)
    assert main(["sync", "--repo", str(target)]) == 0
    capsys.readouterr()
    main(["doctor", "--repo", str(target), "--skip-probe"])
    out = capsys.readouterr().out
    assert f"backup: remote={remote} auto=True" in out
    assert "last_push=" in out and " ok @ " in out


@requires_git
def test_doctor_backup_line_compresses_and_survives_corrupt_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """doctor's backup line stays ONE line under a recorded oversized/multi-line push error (first
    line only, truncated at 140 chars with '…'), and a corrupted _kb/backup.json — wrong types or
    broken JSON — degrades to last_push=unreadable, NEVER a doctor crash (doctor's whole job is
    diagnosing broken repos)."""
    target = tmp_path / "kb"
    assert main(["repo", "init", str(target)]) == 0
    layout = RepoLayout(target)
    _set_backup(layout, remote="git@example.com:me/kb.git")
    state = layout.kb_dir / "backup.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "remote": "git@example.com:me/kb.git",
                "ok": False,
                "at": "2026-07-24T00:00:00Z",
                "commit": None,
                "error": ("E" * 200) + "\nfatal: second stderr line",
            }
        ),
        encoding="utf-8",
    )
    capsys.readouterr()

    main(["doctor", "--repo", str(target), "--skip-probe"])
    out = capsys.readouterr().out
    [line] = [ln for ln in out.splitlines() if ln.lstrip().startswith("backup: remote=")]
    assert "FAILED (" in line
    assert "…" in line  # the 140-char truncation fired
    assert "second stderr line" not in out  # later stderr lines never reach the report

    # Wrong-typed fields (a non-string commit) and non-JSON bytes both degrade, never crash.
    for corrupt in ('{"ok": true, "commit": 12345}', "{not json"):
        state.write_text(corrupt, encoding="utf-8")
        capsys.readouterr()
        main(["doctor", "--repo", str(target), "--skip-probe"])
        assert "last_push=unreadable" in capsys.readouterr().out


# --- eval (the deterministic ranking snapshot, issue #44) ---------------------------------------
_EVAL_INDEX = """\
# personal

- [Eng MOC](wiki/eng/eng-moc.md)
"""

_EVAL_MOC = """\
---
status: active
type: moc
title: eng MOC
summary: Map of content for the eng domain.
---
# eng MOC

- [Deadlock recovery](themes/deadlock-recovery.md)
"""

_EVAL_THEME = """\
---
status: active
type: theme
tags: [locking]
---
# Deadlock recovery

A deadlock is recovered by dropping the younger advisory lock and letting the older writer
proceed; recovery is deterministic and leaves no partial write.
"""


def _eval_repo(root: Path) -> Path:
    """A minimal v1-layout KB — no git, no config, no model. `agora eval` needs none of them."""
    (root / "wiki" / "eng" / "themes").mkdir(parents=True)
    (root / "index.md").write_text(_EVAL_INDEX, encoding="utf-8")
    (root / "wiki" / "eng" / "eng-moc.md").write_text(_EVAL_MOC, encoding="utf-8")
    (root / "wiki" / "eng" / "themes" / "deadlock-recovery.md").write_text(
        _EVAL_THEME, encoding="utf-8"
    )
    return root


def _eval_queries(path: Path, *, expect_unrelated: str = "not_found") -> Path:
    path.write_text(
        "- id: q-deadlock\n"
        "  question: deadlock recovery\n"
        "  expect: ok\n"
        "- id: q-unrelated\n"
        "  question: quantum biology photosynthesis\n"
        f"  expect: {expect_unrelated}\n",
        encoding="utf-8",
    )
    return path


def test_eval_prints_a_table_and_exits_zero_when_expects_hold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The happy path: every query matched its declared expect, so `agora eval` is a green gate."""
    repo = _eval_repo(tmp_path / "kb")
    queries = _eval_queries(tmp_path / "q.yaml")
    assert main(["eval", "--repo", str(repo), "--queries", str(queries)]) == 0
    out = capsys.readouterr().out
    assert "fm=" in out and "cache=" in out
    assert "q-deadlock" in out and "deadlock-recovery" in out
    assert "0 violating expect" in out


def test_eval_writes_the_full_json_snapshot_with_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--out` is the golden-file producer: basename identity, no paths, header + per-query hits."""
    repo = _eval_repo(tmp_path / "kb")
    queries = _eval_queries(tmp_path / "q.yaml")
    out = tmp_path / "nested" / "baseline.json"  # parent dirs are created, not demanded
    assert main(["eval", "--repo", str(repo), "--queries", str(queries), "--out", str(out)]) == 0
    assert f"wrote {out}" in capsys.readouterr().out

    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["header"]["query_count"] == 2
    assert record["header"]["corpus_note_count"] == 3
    [deadlock] = [q for q in record["queries"] if q["id"] == "q-deadlock"]
    assert deadlock["status"] == "ok"
    assert deadlock["hits"][0]["note"] == "deadlock-recovery"  # BASENAME, survives a layout move
    assert deadlock["hits"][0]["rank"] == 1
    assert "wiki/" not in out.read_text(encoding="utf-8")


def test_eval_exits_one_and_names_the_query_when_an_expect_is_violated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CI gate: a query whose status differs from its expect fails the command, on stderr."""
    repo = _eval_repo(tmp_path / "kb")
    queries = _eval_queries(tmp_path / "q.yaml", expect_unrelated="ok")
    assert main(["eval", "--repo", str(repo), "--queries", str(queries)]) == 1
    captured = capsys.readouterr()
    assert "MISMATCH: q-unrelated expected ok, got not_found" in captured.err
    assert "1 violating expect" in captured.out


def test_eval_exits_one_on_an_unusable_query_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed / missing query file is loud and non-zero — never a silently smaller eval set."""
    repo = _eval_repo(tmp_path / "kb")
    missing = tmp_path / "nope.yaml"
    assert main(["eval", "--repo", str(repo), "--queries", str(missing)]) == 1
    assert "eval: cannot read query file" in capsys.readouterr().err

    bad = tmp_path / "bad.yaml"
    bad.write_text("id: a\nquestion: x\n", encoding="utf-8")  # a mapping, not a list
    assert main(["eval", "--repo", str(repo), "--queries", str(bad)]) == 1
    assert "expected a LIST" in capsys.readouterr().err


def test_eval_fm_and_limit_flags_reach_the_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--fm off` pins the ADR-0012 §8 Phase-1a column; `--limit` caps hits per query."""
    repo = _eval_repo(tmp_path / "kb")
    queries = _eval_queries(tmp_path / "q.yaml")
    out = tmp_path / "off.json"
    args = ["eval", "--repo", str(repo), "--queries", str(queries), "--out", str(out)]
    assert main([*args, "--fm", "off", "--limit", "1"]) == 0
    capsys.readouterr()
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["header"]["fm_enabled"] is False
    assert record["header"]["limit"] == 1
    assert all(len(q["hits"]) <= 1 for q in record["queries"])

    assert main([*args, "--fm", "on"]) == 0
    capsys.readouterr()
    assert json.loads(out.read_text(encoding="utf-8"))["header"]["fm_enabled"] is True


@pytest.mark.parametrize("limit", ["0", "-3"])
def test_eval_rejects_a_non_positive_limit(tmp_path: Path, limit: str) -> None:
    """`--limit 0` must not be a permanently green gate that records nothing.

    `Wiki.query` fixes `status` from the eligible set BEFORE slicing `eligible[: max(0, limit)]`,
    so `--limit 0` used to exit 0 with `status: ok` and an EMPTY hit list for every query — a
    baseline that pins no ranking while looking green, which a copied CI line or a typo could
    produce silently. argparse refuses it instead (exit 2, the usage-error code).
    """
    repo = _eval_repo(tmp_path / "kb")
    queries = _eval_queries(tmp_path / "q.yaml")
    with pytest.raises(SystemExit) as exc:
        main(["eval", "--repo", str(repo), "--queries", str(queries), "--limit", limit])
    assert exc.value.code == 2


def test_eval_needs_no_git_no_model_and_no_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate must run on a bare directory of markdown — that is what makes it usable in CI.

    `_eval_repo` writes no `.git`, no `_kb/repo.yaml`, and no `adapters.yaml`; an EMPTY directory
    likewise answers `not_found` (ADR-0012 §5 gate (d)) instead of crashing.
    """
    repo = _eval_repo(tmp_path / "kb")
    assert not (repo / ".git").exists()
    queries = _eval_queries(tmp_path / "q.yaml")
    assert main(["eval", "--repo", str(repo), "--queries", str(queries)]) == 0
    capsys.readouterr()

    empty = tmp_path / "empty"
    empty.mkdir()
    all_not_found = _eval_queries(tmp_path / "nf.yaml")
    all_not_found.write_text(
        "- id: q-deadlock\n  question: deadlock recovery\n  expect: not_found\n", encoding="utf-8"
    )
    assert main(["eval", "--repo", str(empty), "--queries", str(all_not_found)]) == 0
