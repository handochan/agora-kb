"""Tests for ``agora repo upgrade --restamp`` — the engine-only frontmatter backfill (#175/#174).

The subject is a **converted** KB: the shape :func:`tests.support.kb_builder.build_kb` already
produces, and the one the owner's real repo is in after a vault import — claim-bearing notes whose
``sources:`` are intact but which carry no ``source_links:`` mirror (the mirror is minted at
CREATE / MERGE / CONTEST and nothing backfilled it) and whose ``tags:`` are empty (the importer
strips any tag absent from the destination taxonomy, which for a fresh repo is ``{}``).

Two properties are load-bearing and every other assertion here is downstream of them:

* the published bytes are **the bytes APPLY would have written** — proven against a note a REAL
  ``worker.run`` published, not against a re-derivation of the mirror rule;
* **nothing outside the frontmatter moves** — proven per note against the base blob's raw body
  region, because the final-diff gate is path-level and cannot see a mangled body.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

import agora_kb.curator.restamp as restamp_mod
from agora_kb.config import KbIdentity, load_repo_config, write_kb_identity
from agora_kb.core import frontmatter
from agora_kb.core.inbox import Inbox
from agora_kb.core.layout import RepoLayout
from agora_kb.core.repo import Repo
from agora_kb.core.state import StateStore
from agora_kb.curator.apply import region_sentinel_id
from agora_kb.curator.claim import LockHeld, curator_lock
from agora_kb.curator.restamp import (
    CHANGEABLE_KEYS,
    SKIP_NOT_ROUND_TRIP,
    TAXONOMY_REL_PATH,
    TagMatch,
    plan_restamp,
    run_restamp,
)
from agora_kb.curator.worker import FakeBackend
from agora_kb.curator.worker import run as worker_run
from agora_kb.ingest.vault_tags import build_vault_tag_index
from agora_kb.schema.emit import Taxonomy, emit_schema
from agora_kb.schema.lint import SOURCE_LINKS_KEY
from tests.support.kb_builder import FIXTURE_KB_ID, NoteSpec, build_kb

NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)
INIT_AT = datetime(2026, 1, 16, 0, 0, 0, tzinfo=UTC)

CONCEPT_A = "wiki/concepts/curator-concurrency.md"
CONCEPT_B = "wiki/concepts/retrieval-augmented-generation.md"
CONCEPT_STUB = "wiki/concepts/sourceless-stub.md"
JOURNAL = "wiki/notes/2026/01/2026-01-15.md"
MAP_AI = "wiki/maps/ai-tech.md"

#: A corpus with one of every tier the selector must get right: two sourced concepts (which acquire
#: a mirror), one STUB concept with empty ``sources:`` (which legitimately acquires none), a journal
#: and the maps/index the builder generates (which must never appear in the diff at all).
_CORPUS = [
    NoteSpec(
        kind="theme",
        domain="ai-tech",
        title="Curator Concurrency",
        body="One curator advances the curated branch under a per-repo flock.",
    ),
    NoteSpec(
        kind="theme",
        domain="ai-tech",
        title="Retrieval Augmented Generation",
        body="Retrieval augmented generation grounds an answer in retrieved documents.",
    ),
    NoteSpec(
        kind="theme",
        domain="general",
        title="Sourceless Stub",
        body="A stub concept carries no sources yet, and therefore no mirror.",
        slug="sourceless-stub",
        status="stub",
        sources=[],
    ),
    NoteSpec(
        kind="daily",
        domain="ai-tech",
        title="Consolidation journal",
        body="What the run of 2026-01-15 consolidated.",
        slug="ai-tech-2026-01-15",
        extra_frontmatter={"date": "2026-01-15"},
    ),
]


# --- fixtures -----------------------------------------------------------------------------------
def _converted_repo(root: Path, notes: list[NoteSpec] | None = None) -> Repo:
    """A git schema-2 KB in the CONVERTED shape: no ``source_links:`` anywhere, every ``tags: []``.

    ``build_kb`` writes no mirror at all, which is exactly the post-import state — so the fixture
    states the problem rather than manufacturing it by deleting a key.
    """
    build_kb(root, notes if notes is not None else _CORPUS, schema_version=2, kb_id=FIXTURE_KB_ID)
    repo = Repo(RepoLayout(root))
    repo.init(when=INIT_AT, schema_version=2, kb_id=FIXTURE_KB_ID)
    return repo


def _restamp(
    repo: Repo,
    *,
    tag_source: object | None = None,
    dry_run: bool = False,
    now: datetime = NOW,
    taxonomy: Taxonomy | None = None,
):
    """Invoke the engine with the taxonomy the repo actually declares (what the face will pass)."""
    tax = taxonomy if taxonomy is not None else load_repo_config(repo.layout).taxonomy
    return run_restamp(repo, taxonomy=tax, now=now, tag_source=tag_source, dry_run=dry_run)  # type: ignore[arg-type]


def _fm(repo: Repo, rel: str) -> dict[str, object]:
    parsed, _body = frontmatter.parse((repo.root / rel).read_text(encoding="utf-8"))
    return parsed


def _blob(repo: Repo, commit: str, rel: str) -> str:
    """The file's text AT ``commit`` — the pre-image the body-invariance claim is about."""
    import subprocess

    out = subprocess.run(  # noqa: S603
        ["git", "show", f"{commit}:{rel}"], cwd=repo.root, capture_output=True, check=True
    )
    return out.stdout.decode("utf-8")


def _raw_body(text: str) -> str:
    """Everything after the closing ``---`` fence, unnormalised (the test's own reader)."""
    rest = text.split("\n", 1)[1]
    marker = "\n---\n"
    return rest[rest.index(marker) + len(marker) :]


def _changed_paths(repo: Repo, commit: str) -> set[str]:
    import subprocess

    out = subprocess.run(  # noqa: S603
        ["git", "show", "--name-only", "--pretty=format:", commit],
        cwd=repo.root,
        capture_output=True,
        check=True,
        text=True,
    )
    return {line for line in out.stdout.splitlines() if line}


def _tree_bytes(repo: Repo) -> dict[str, bytes]:
    """Every non-``.git`` file in the repo, by path → bytes. The "wrote nothing" oracle."""
    return {
        p.relative_to(repo.root).as_posix(): p.read_bytes()
        for p in repo.root.rglob("*")
        if p.is_file() and not p.relative_to(repo.root).as_posix().startswith(".git/")
    }


class _FakeTagSource:
    """A ``TagSource`` with no filesystem behind it — the seam the engine is written against."""

    def __init__(self, matches: dict[str, TagMatch]) -> None:
        self._matches = matches

    def lookup(self, basename: str) -> TagMatch:
        return self._matches.get(basename, TagMatch(status="unmatched"))


# --- (1) the APPLY byte-identity claim ----------------------------------------------------------
_APPLY_TAXONOMY = Taxonomy(
    schema_version=2,
    taxonomy_policy="open",
    allowed_tags=("curator", "concurrency"),
    domains=("ai-tech", "general"),
)


def _apply_published_repo(root: Path) -> tuple[Repo, str]:
    """Publish ONE concept through the REAL curator run, and return the repo + its note path."""
    layout = RepoLayout(root)
    layout.root.mkdir(parents=True, exist_ok=True)
    write_kb_identity(layout, KbIdentity(kb_id=FIXTURE_KB_ID, name="agora-fixture"))
    for directory in ("concepts", "summaries", "notes", "maps", "entities", "people"):
        (layout.wiki_dir / directory).mkdir(parents=True, exist_ok=True)
        (layout.wiki_dir / directory / ".gitkeep").write_text("", encoding="utf-8")
    repo = Repo(layout)
    repo.init(when=INIT_AT, schema_version=2, kb_id=FIXTURE_KB_ID)
    emit_schema(layout, taxonomy=_APPLY_TAXONOMY, schema_version=2)

    event_id = (
        Inbox(layout)
        .write(
            text="One curator advances the branch under a lock.",
            writer="dochan",
            source="claude-code",
            domain="ai-tech",
            now=datetime(2026, 9, 6, 2, 40, 10, tzinfo=UTC),
        )
        .id
    )
    raw = layout.root / "raw" / "ai-tech" / f"{event_id}.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(f"raw capture {event_id}\n", encoding="utf-8")
    repo.commit_worktree(repo.root, "chore: emit schema + seed raw", when=INIT_AT)

    plan = json.dumps(
        {
            "schema_version": 1,
            "run_id": "ignored",
            "finished": True,
            "dispositions": [
                {
                    "candidate_id": "c1",
                    "event_ids": [event_id],
                    "op": "CREATE_THEME",
                    "domain": "ai-tech",
                    "basename": "curator-concurrency",
                    "title": "Curator concurrency model",
                    "summary": "One curator advances the curated branch under a per-repo lock.",
                    "status": "active",
                    "tags": ["curator", "concurrency"],
                    "aliases": [],
                    "links": [],
                    "needs_prose": True,
                    "reason": "New concept.",
                }
            ],
        }
    )
    report = worker_run(
        repo,
        backend=FakeBackend(
            plan,
            prose={
                region_sentinel_id("ignored", "c1"): "The single curator holds a per-repo lock."
            },
        ),
        state_store=StateStore(layout),
        now=NOW,
        taxonomy=_APPLY_TAXONOMY,
    )
    assert report.status == "published", report
    return repo, CONCEPT_A


def test_restamped_bytes_are_the_bytes_apply_wrote(tmp_path: Path) -> None:
    """The whole design in one assertion: publish through APPLY, delete the mirror, restamp, and
    the file is byte-identical to APPLY's own output.

    True BY DEFINITION rather than by coincidence, because the mirror is stamped by APPLY's own
    :func:`~agora_kb.curator.apply._stamp_source_links` at a claim-bearing call site — but only this
    end-to-end comparison proves the SURROUNDING render (key order, quoting, the body) also agrees.
    """
    repo, rel = _apply_published_repo(tmp_path)
    apply_bytes = (repo.root / rel).read_text(encoding="utf-8")
    assert SOURCE_LINKS_KEY in frontmatter.parse(apply_bytes)[0]

    fm, body = frontmatter.parse(apply_bytes)
    fm.pop(SOURCE_LINKS_KEY)
    (repo.root / rel).write_text(frontmatter.render(fm, body), encoding="utf-8")
    repo.commit_worktree(
        repo.root, "chore: drop the mirror (simulate the converted shape)", when=INIT_AT
    )
    assert (repo.root / rel).read_text(encoding="utf-8") != apply_bytes

    report = _restamp(repo, taxonomy=_APPLY_TAXONOMY)

    assert report.status == "published"
    assert (repo.root / rel).read_text(encoding="utf-8") == apply_bytes


# --- (2)(3)(4) the frontmatter-only contract ------------------------------------------------------
def test_the_mirror_lands_on_sourced_concepts_only(tmp_path: Path) -> None:
    repo = _converted_repo(tmp_path)
    base = repo.branch_commit()

    report = _restamp(repo)

    assert report.status == "published"
    assert _fm(repo, CONCEPT_A)[SOURCE_LINKS_KEY] == ["[[raw/ai-tech/curator-concurrency.md]]"]
    assert _fm(repo, CONCEPT_B)[SOURCE_LINKS_KEY] == [
        "[[raw/ai-tech/retrieval-augmented-generation.md]]"
    ]
    # A concept whose `sources:` holds no raw/ entry gets NO key — an empty mirror is popped, never
    # left behind as `source_links: []` to go stale.
    assert SOURCE_LINKS_KEY not in _fm(repo, CONCEPT_STUB)
    assert SOURCE_LINKS_KEY not in _fm(repo, JOURNAL)
    assert SOURCE_LINKS_KEY not in _fm(repo, MAP_AI)
    assert SOURCE_LINKS_KEY not in _fm(repo, "index.md")

    stub = next(n for n in report.plan.notes if n.rel_path == CONCEPT_STUB)
    assert stub.changed is False and stub.skipped is None
    assert _changed_paths(repo, repo.branch_commit()) == {CONCEPT_A, CONCEPT_B, "log.md"}
    assert base != repo.branch_commit()


def test_the_mirror_is_written_immediately_after_sources(tmp_path: Path) -> None:
    """Key ORDER, not just presence: the two render adjacent so a source change is one diff hunk."""
    repo = _converted_repo(tmp_path)

    _restamp(repo)

    keys = list(_fm(repo, CONCEPT_A))
    assert keys.index(SOURCE_LINKS_KEY) == keys.index("sources") + 1


def test_no_body_byte_moves_on_any_note(tmp_path: Path) -> None:
    """The obligation with no downstream backstop: the final-diff gate is PATH-level, so a mangled
    body under ``wiki/`` would pass every gate but this one."""
    repo = _converted_repo(tmp_path)
    base = repo.branch_commit()

    _restamp(repo)

    for rel in (CONCEPT_A, CONCEPT_B, CONCEPT_STUB, JOURNAL, MAP_AI, "index.md"):
        after = (repo.root / rel).read_text(encoding="utf-8")
        assert _raw_body(after) == _raw_body(_blob(repo, base, rel)), rel


def test_only_the_two_changeable_keys_differ(tmp_path: Path) -> None:
    repo = _converted_repo(tmp_path)
    base = repo.branch_commit()
    source = _FakeTagSource({"curator-concurrency": TagMatch("matched", tags=("infra",))})

    _restamp(repo, tag_source=source)

    for rel in (CONCEPT_A, CONCEPT_B):
        before, _ = frontmatter.parse(_blob(repo, base, rel))
        after, _ = frontmatter.parse((repo.root / rel).read_text(encoding="utf-8"))
        differing = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        assert differing <= CHANGEABLE_KEYS, (rel, differing)


def test_a_round_trip_unstable_note_is_skipped_and_left_alone(tmp_path: Path) -> None:
    """A hand-edited flow-style ``tags: [a, b]`` does not survive a re-render byte-for-byte, so the
    note is REPORTED and untouched rather than silently reformatted."""
    repo = _converted_repo(tmp_path)
    path = repo.root / CONCEPT_B
    text = path.read_text(encoding="utf-8").replace("tags: []\n", "tags: [ ]   # hand-edited\n")
    path.write_text(text, encoding="utf-8")
    repo.commit_worktree(repo.root, "chore: hand-edit a note", when=INIT_AT)

    report = _restamp(repo)

    skipped = next(n for n in report.plan.notes if n.rel_path == CONCEPT_B)
    assert skipped.skipped == SKIP_NOT_ROUND_TRIP
    assert skipped.changed is False
    assert path.read_text(encoding="utf-8") == text
    assert CONCEPT_B not in _changed_paths(repo, repo.branch_commit())


# --- (5)(6) the selector -------------------------------------------------------------------------
def test_a_template_declaring_kind_concept_is_never_selected(tmp_path: Path) -> None:
    """The trap a naive ``rglob("*.md")`` + ``fm['kind']`` selector falls into.

    ``_templates/concept.md`` really does carry ``kind: concept`` in its frontmatter, and
    ``_templates/`` is outside the curator allowlist — so an rglob-shaped implementation would pull
    it into the commit and then fail its own final-diff gate. ``scan_live_tree`` scans ``index.md``
    + ``wiki/**`` only, so the trap is unreachable rather than merely avoided.
    """
    repo = _converted_repo(tmp_path)
    template = repo.root / "_templates" / "concept.md"
    assert "kind: concept" in template.read_text(encoding="utf-8")
    before = template.read_text(encoding="utf-8")

    report = _restamp(repo)

    assert all("_templates" not in n.rel_path for n in report.plan.notes)
    assert not any(p.startswith("_templates/") for p in _changed_paths(repo, repo.branch_commit()))
    assert template.read_text(encoding="utf-8") == before


def test_raw_is_never_written(tmp_path: Path) -> None:
    repo = _converted_repo(tmp_path)
    raw_before = {
        p.relative_to(repo.root).as_posix(): p.read_bytes()
        for p in (repo.root / "raw").rglob("*")
        if p.is_file()
    }
    assert raw_before

    _restamp(repo)

    raw_after = {
        p.relative_to(repo.root).as_posix(): p.read_bytes()
        for p in (repo.root / "raw").rglob("*")
        if p.is_file()
    }
    assert raw_after == raw_before
    assert not any(p.startswith("raw/") for p in _changed_paths(repo, repo.branch_commit()))


# --- (7) idempotence -----------------------------------------------------------------------------
def test_a_second_run_is_a_true_no_op(tmp_path: Path) -> None:
    """No commit, no ``log.md`` growth, ``status='noop'``.

    Only reachable because the log entry is appended AFTER the ``plan.changed`` short-circuit: write
    it first and the diff is never empty, so a second run could never be honest.
    """
    repo = _converted_repo(tmp_path)
    _restamp(repo)
    tip = repo.branch_commit()
    log_bytes = (repo.root / "log.md").read_bytes()

    report = _restamp(repo)

    assert report.status == "noop"
    assert report.published_commit is None
    assert repo.branch_commit() == tip
    assert (repo.root / "log.md").read_bytes() == log_bytes


# --- (8)(9)(10) the lock and the preview ----------------------------------------------------------
@pytest.mark.skipif(os.name == "nt", reason="curator_lock is fcntl/POSIX")
def test_a_held_lock_refuses_and_changes_nothing(tmp_path: Path) -> None:
    repo = _converted_repo(tmp_path)
    tip = repo.branch_commit()
    before = (repo.root / CONCEPT_A).read_bytes()

    with curator_lock(repo.layout), pytest.raises(LockHeld):
        _restamp(repo)

    assert repo.branch_commit() == tip
    assert (repo.root / CONCEPT_A).read_bytes() == before


def test_dry_run_writes_nothing_at_all_not_even_kb(tmp_path: Path) -> None:
    """Taking the lock CREATES ``_kb/`` + a 0-byte ``curator.lock``, and ``git worktree add`` writes
    into ``.git/worktrees/`` — so a preview that took either would visibly mutate the repo it
    promised to leave alone. It takes neither."""
    repo = _converted_repo(tmp_path)
    tip = repo.branch_commit()
    assert not (repo.root / "_kb").exists()
    before = _tree_bytes(repo)

    report = _restamp(repo, dry_run=True)

    assert report.status == "dry-run"
    assert report.plan.changed is True
    assert _tree_bytes(repo) == before
    assert not (repo.root / "_kb").exists()
    assert not (repo.root / ".git" / "worktrees").exists()
    assert repo.branch_commit() == tip
    assert SOURCE_LINKS_KEY not in _fm(repo, CONCEPT_A)


@pytest.mark.skipif(os.name == "nt", reason="curator_lock is fcntl/POSIX")
def test_dry_run_warns_when_a_curator_run_is_in_progress(tmp_path: Path) -> None:
    """The preview does not REFUSE on a held lock (it takes none), so it says so instead."""
    repo = _converted_repo(tmp_path)

    with curator_lock(repo.layout):
        report = _restamp(repo, dry_run=True)

    assert report.status == "dry-run"
    assert any("curator run is in progress" in w for w in report.warnings), report.warnings


def test_the_preview_equals_the_result(tmp_path: Path) -> None:
    """One planner, two mounts — so ``--dry-run`` is a promise, not a second implementation."""
    repo = _converted_repo(tmp_path)

    preview = _restamp(repo, dry_run=True)
    real = _restamp(repo)

    assert real.status == "published"
    assert preview.plan.notes == real.plan.notes
    assert preview.plan.taxonomy_added == real.plan.taxonomy_added


# --- (11)(12)(13) the ADMIN final-diff gate -------------------------------------------------------
def _plant(monkeypatch: pytest.MonkeyPatch, plant: object) -> None:
    """Run ``plant(worktree)`` just before the final-diff gate, from inside the real run.

    Hooked on the ``log.md`` append because that is the last step before the gate: whatever the
    plant does is therefore graded by the gate and by nothing else, which is what these cases are
    about.
    """
    original = restamp_mod._append_upgrade_log

    def hooked(worktree: Path, **kwargs: object) -> None:
        original(worktree, **kwargs)  # type: ignore[arg-type]
        plant(worktree)  # type: ignore[operator]

    monkeypatch.setattr(restamp_mod, "_append_upgrade_log", hooked)


@pytest.mark.parametrize(
    ("rel", "text"),
    [
        ("raw/ai-tech/planted.md", "a forged capture\n"),
        ("wiki/people/hando/note.md", "---\ntitle: mine\n---\n\nhuman-owned\n"),
        ("_meta/kb.yaml", "kb_id: forged\n"),
        ("_templates/concept.md", "---\ntitle: t\n---\n\nrewritten\n"),
    ],
)
def test_an_off_allowlist_write_refuses_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rel: str, text: str
) -> None:
    """``raw/``, the human-owned ``wiki/people/**``, ``_meta/`` and ``_templates/`` all stay out.

    None of the four needs its own check: :func:`~agora_kb.curator.constants.is_allowlisted_path`
    already denies them (``wiki/people/`` by the D3.3 deny-prefix, tested BEFORE the allow test),
    and a restamp has no engine-written-``raw/`` concept to admit anything through.
    """
    repo = _converted_repo(tmp_path)
    tip = repo.branch_commit()

    def plant(worktree: Path) -> None:
        path = worktree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    _plant(monkeypatch, plant)
    report = _restamp(repo)

    assert report.status == "refused"
    assert any("RESTAMP-DIFF" in r and rel in r for r in report.reasons), report.reasons
    assert repo.branch_commit() == tip
    assert SOURCE_LINKS_KEY not in _fm(repo, CONCEPT_A)


def test_the_taxonomy_is_admitted_only_as_the_bytes_this_run_computed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authorship-then-bytes, exactly as ``_is_engine_written_raw`` admits an engine ``raw/`` write:
    tampering with the file after the run computed it drops it back off the allowlist."""
    repo = _converted_repo(tmp_path)
    tip = repo.branch_commit()
    vault = tmp_path / "vault"
    (vault / "curator-concurrency.md").parent.mkdir(parents=True, exist_ok=True)
    (vault / "curator-concurrency.md").write_text(
        "---\ntitle: C\ntags: [infra]\n---\n\n# C\n", encoding="utf-8"
    )

    _plant(
        monkeypatch,
        lambda wt: (wt / TAXONOMY_REL_PATH).write_text("allowed_tags: {}\n", encoding="utf-8"),
    )
    report = _restamp(repo, tag_source=build_vault_tag_index(vault))

    assert report.status == "refused"
    assert any(TAXONOMY_REL_PATH in r for r in report.reasons), report.reasons
    assert repo.branch_commit() == tip


def test_a_run_without_tag_recovery_may_not_touch_the_taxonomy_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``taxonomy_write is None`` admits nothing: the widened gate exists only for the run that
    needs it."""
    repo = _converted_repo(tmp_path)

    _plant(
        monkeypatch,
        lambda wt: (wt / TAXONOMY_REL_PATH).write_text("schema_version: 2\n", encoding="utf-8"),
    )
    report = _restamp(repo)

    assert report.status == "refused"
    assert any(TAXONOMY_REL_PATH in r for r in report.reasons), report.reasons


# --- (14)(15) CAS and state -----------------------------------------------------------------------
def test_a_lost_cas_publishes_nothing_and_writes_no_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _converted_repo(tmp_path)
    tip = repo.branch_commit()
    monkeypatch.setattr(Repo, "compare_and_swap_branch", lambda *a, **k: False)

    report = _restamp(repo)

    assert report.status == "conflict"
    assert report.published_commit is None
    assert repo.branch_commit() == tip
    assert not repo.layout.state_file.exists()


def test_a_published_run_writes_no_curator_state(tmp_path: Path) -> None:
    """Every field a published INGEST run records would be a lie here — ``published_runs`` has no
    manifest to key on, ``last_batch`` describes a claim that never happened, and ``last_run`` is an
    INPUT to the trigger evaluator, so stamping it would silently postpone the next consolidation.
    The receipts are ``log.md`` and the commit."""
    repo = _converted_repo(tmp_path)
    assert not repo.layout.state_file.exists()

    report = _restamp(repo)

    assert report.status == "published"
    assert not repo.layout.state_file.exists()


# --- (16) the lint gate ---------------------------------------------------------------------------
def test_lint_blocks_the_publish_when_a_recovered_tag_is_not_in_allowed_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression test for the D10 ORDERING hazard.

    L1-5 (``tags`` ⊆ ``allowed_tags``) is an ERROR. If the lint gate is handed the taxonomy the
    caller loaded BEFORE the union instead of the merged one, every tag this run just recovered
    fails it and the run refuses itself. Simulated by neutering the union that feeds ``lint``, which
    must then reject — proving the gate is real and that the correct value is what makes it pass.
    """
    repo = _converted_repo(tmp_path)
    tip = repo.branch_commit()
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "curator-concurrency.md").write_text(
        "---\ntitle: C\ntags: [infra]\n---\n\n# C\n", encoding="utf-8"
    )
    monkeypatch.setattr(restamp_mod, "_unioned_taxonomy", lambda taxonomy, plan: taxonomy)

    report = _restamp(repo, tag_source=build_vault_tag_index(vault))

    assert report.status == "refused"
    assert any("LINT L1-5" in r for r in report.reasons), report.reasons
    assert repo.branch_commit() == tip
    assert report.lint is not None and not report.lint.ok


# --- (17) taxonomy_policy -------------------------------------------------------------------------
def _repo_with_policy(root: Path, policy: str) -> Repo:
    build_kb(root, _CORPUS, schema_version=2, kb_id=FIXTURE_KB_ID)
    path = root / TAXONOMY_REL_PATH
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["taxonomy_policy"] = policy
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    repo = Repo(RepoLayout(root))
    repo.init(when=INIT_AT, schema_version=2, kb_id=FIXTURE_KB_ID)
    return repo


def _vault(root: Path, **notes: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for basename, tags in notes.items():
        (root / f"{basename}.md").write_text(
            f"---\ntitle: {basename}\ntags: [{tags}]\n---\n\n# {basename}\n", encoding="utf-8"
        )
    return root


@pytest.mark.parametrize(
    ("policy", "tags", "expected"),
    [
        ("open", "infra, agent", "published"),
        ("review-only", "infra", "refused"),
        ("capped:2", "infra, agent", "published"),
        ("capped:2", "infra, agent, howto", "refused"),
        ("capped:oops", "infra", "refused"),
        ("shout", "infra", "refused"),
    ],
)
def test_taxonomy_policy_gates_the_evolution(
    tmp_path: Path, policy: str, tags: str, expected: str
) -> None:
    """``--tags-from-vault`` IS the §5.2 admin evolution path, so it is the first thing in this
    build that enforces ``taxonomy_policy`` — the one L1 rule ``lint()`` documents itself as unable
    to evaluate, because it needs the (before, after) pair this run happens to hold."""
    repo = _repo_with_policy(tmp_path / "kb", policy)
    source = build_vault_tag_index(_vault(tmp_path / "vault", **{"curator-concurrency": tags}))

    report = _restamp(repo, tag_source=source)

    assert report.status == expected, report.reasons
    if expected == "refused":
        assert any("TAXONOMY-POLICY" in r for r in report.reasons), report.reasons


def test_review_only_still_permits_a_run_that_adds_no_tag(tmp_path: Path) -> None:
    """No evolution happened, so there is nothing for an anti-sprawl gate to grade — the
    ``source_links:`` half must not be held hostage by a policy about tags."""
    repo = _repo_with_policy(tmp_path / "kb", "review-only")

    report = _restamp(repo)

    assert report.status == "published"
    assert _fm(repo, CONCEPT_A)[SOURCE_LINKS_KEY]


# --- (18) non-kebab tags --------------------------------------------------------------------------
def test_a_non_kebab_tag_is_reported_and_never_enters_the_taxonomy(tmp_path: Path) -> None:
    """L1-5 checks MEMBERSHIP only, so ``AI/Tech`` in ``allowed_tags`` lints CLEAN while leaving a
    repo whose own ``InboxItem`` validator refuses every capture carrying it. The note's tag leg is
    skipped and reported; its mirror still lands, because #174's data quality must not block #175.
    """
    repo = _converted_repo(tmp_path)
    source = _FakeTagSource(
        {"curator-concurrency": TagMatch("matched", tags=("Agent",), invalid_tags=("Agent",))}
    )

    report = _restamp(repo, tag_source=source)

    assert report.status == "published"
    assert report.plan.taxonomy_added == ()
    assert _fm(repo, CONCEPT_A)["tags"] == []
    assert _fm(repo, CONCEPT_A)[SOURCE_LINKS_KEY]
    change = next(n for n in report.plan.notes if n.rel_path == CONCEPT_A)
    assert change.tag_match is not None
    assert change.tag_match.invalid_tags == ("Agent",)
    doc = yaml.safe_load((repo.root / TAXONOMY_REL_PATH).read_text(encoding="utf-8"))
    assert doc["allowed_tags"] == {}


# --- the tag-recovery leg end to end --------------------------------------------------------------
def test_tags_are_recovered_by_basename_and_the_taxonomy_widens_atomically(tmp_path: Path) -> None:
    """One commit carries the recovered ``tags:`` AND the ``allowed_tags`` keys they need.

    Splitting them would leave a crash window in which a note carries a tag the taxonomy does not
    declare — after which EVERY curate run fails L1-5, permanently. §5 requires the key and its
    first use to land together, and that is the only reason this gate is widened at all.
    """
    repo = _converted_repo(tmp_path)
    vault = _vault(
        tmp_path / "vault",
        **{
            "curator-concurrency": "infra, agent",
            "retrieval-augmented-generation": "agent, technique",
        },
    )
    (vault / "sourceless-stub.md").write_text("---\ntitle: s\n---\n\n# s\n", encoding="utf-8")

    report = _restamp(repo, tag_source=build_vault_tag_index(vault))

    assert report.status == "published"
    assert _fm(repo, CONCEPT_A)["tags"] == ["infra", "agent"]
    assert _fm(repo, CONCEPT_B)["tags"] == ["agent", "technique"]
    assert report.plan.taxonomy_added == ("agent", "infra", "technique")
    doc = yaml.safe_load((repo.root / TAXONOMY_REL_PATH).read_text(encoding="utf-8"))
    assert doc["allowed_tags"] == {"agent": {}, "infra": {}, "technique": {}}
    assert _changed_paths(repo, repo.branch_commit()) == {
        CONCEPT_A,
        CONCEPT_B,
        TAXONOMY_REL_PATH,
        "log.md",
    }


def test_the_four_match_outcomes_are_reported_separately(tmp_path: Path) -> None:
    repo = _converted_repo(tmp_path)
    vault = tmp_path / "vault"
    (vault / "a").mkdir(parents=True)
    (vault / "b").mkdir(parents=True)
    _vault(vault, **{"curator-concurrency": "infra"})
    (vault / "sourceless-stub.md").write_text("---\ntitle: s\n---\n\n# s\n", encoding="utf-8")
    for side in ("a", "b"):
        (vault / side / "retrieval-augmented-generation.md").write_text(
            f"---\ntitle: {side}\ntags: [agent]\n---\n\n# {side}\n", encoding="utf-8"
        )

    report = _restamp(repo, tag_source=build_vault_tag_index(vault), dry_run=True)

    by_path = {n.rel_path: n for n in report.plan.notes}
    assert by_path[CONCEPT_A].tag_match is not None
    assert by_path[CONCEPT_A].tag_match.status == "matched"
    assert by_path[CONCEPT_STUB].tag_match is not None
    assert by_path[CONCEPT_STUB].tag_match.status == "no-tags"
    ambiguous = by_path[CONCEPT_B].tag_match
    assert ambiguous is not None and ambiguous.status == "ambiguous"
    assert by_path[CONCEPT_B].tags_after == ()  # never guessed


def test_an_unmatched_basename_is_reported_and_keeps_its_mirror(tmp_path: Path) -> None:
    repo = _converted_repo(tmp_path)
    source = build_vault_tag_index(_vault(tmp_path / "vault", **{"curator-concurrency": "infra"}))

    report = _restamp(repo, tag_source=source)

    change = next(n for n in report.plan.notes if n.rel_path == CONCEPT_B)
    assert change.tag_match is not None and change.tag_match.status == "unmatched"
    assert change.changed is True  # the mirror leg still ran
    assert _fm(repo, CONCEPT_B)["tags"] == []


def test_the_union_declares_a_recovered_tag_the_note_already_carried(tmp_path: Path) -> None:
    """The union is over the tags the run LEAVES ON each note, not only the ones it newly writes.

    A partially-repaired repo (the tag is on the note, the key is missing from ``allowed_tags``)
    fails L1-5 on every curate run. This command holds the (before, after) pair, so the widening
    must cover a note whose ``tags:`` did not change — otherwise the run walks past a break it had
    everything needed to fix, and then refuses itself at its own lint gate.
    """
    root = tmp_path / "kb"
    corpus = [*_CORPUS]
    corpus[0] = NoteSpec(
        kind="theme",
        domain="ai-tech",
        title="Curator Concurrency",
        body="One curator advances the curated branch under a per-repo flock.",
        tags=["infra"],
    )
    build_kb(root, corpus, schema_version=2, kb_id=FIXTURE_KB_ID)
    path = root / TAXONOMY_REL_PATH
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc[
        "allowed_tags"
    ] = {}  # the break: the note carries `infra`, the taxonomy does not declare it
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    repo = Repo(RepoLayout(root))
    repo.init(when=INIT_AT, schema_version=2, kb_id=FIXTURE_KB_ID)
    source = build_vault_tag_index(_vault(tmp_path / "vault", **{"curator-concurrency": "infra"}))

    report = _restamp(repo, tag_source=source)

    assert report.status == "published", report.reasons
    assert report.plan.taxonomy_added == ("infra",)
    change = next(n for n in report.plan.notes if n.rel_path == CONCEPT_A)
    assert change.tags_before == change.tags_after == ("infra",)


def test_the_union_preserves_an_existing_descriptor_and_the_mapping_shape(tmp_path: Path) -> None:
    """ "Append-only" means the VALUE and POSITION of an existing key survive: §5 admits a per-tag
    descriptor, and routing the write through ``Taxonomy`` (whose ``allowed_tags`` is a tuple of
    NAMES) would flatten every one of them to ``{}``."""
    root = tmp_path / "kb"
    build_kb(root, _CORPUS, schema_version=2, kb_id=FIXTURE_KB_ID)
    path = root / TAXONOMY_REL_PATH
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["allowed_tags"] = {"zeta": {"desc": "kept"}, "alpha": {}}
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    repo = Repo(RepoLayout(root))
    repo.init(when=INIT_AT, schema_version=2, kb_id=FIXTURE_KB_ID)
    source = build_vault_tag_index(_vault(tmp_path / "vault", **{"curator-concurrency": "infra"}))

    report = _restamp(repo, tag_source=source)

    assert report.status == "published"
    after = yaml.safe_load((root / TAXONOMY_REL_PATH).read_text(encoding="utf-8"))
    # Existing keys keep their value AND their position; the new key is appended.
    assert list(after["allowed_tags"]) == ["zeta", "alpha", "infra"]
    assert after["allowed_tags"]["zeta"] == {"desc": "kept"}
    assert after["allowed_tags"]["infra"] == {}
    assert list(after) == ["schema_version", "taxonomy_policy", "domains", "allowed_tags"]


# --- (19)(20) the receipts -----------------------------------------------------------------------
def test_the_log_entry_parses_as_a_dispositionless_run(tmp_path: Path) -> None:
    """The dashboard's own parser must read the entry without inventing an op.

    ``AgoraHandlers._recent_log`` turns ``- dispositions:`` into the ops timeline, so a maintenance
    count written there would inject names outside the closed ADR-0011 op vocabulary. It ignores
    bullets it does not recognise, which is what makes ``- upgrade: restamp`` safe.
    """
    from agora_kb.faces.mcp_server import AgoraHandlers

    repo = _converted_repo(tmp_path)
    # A converted repo has no run log at all; the entry seeds the same `# Curator log` header the
    # INGEST writer seeds, so the file stays ONE parseable format from its first line.
    assert not (repo.root / "log.md").exists()

    report = _restamp(repo)

    text = (repo.root / "log.md").read_text(encoding="utf-8")
    assert text.startswith("# Curator log\n")
    entry = text[len("# Curator log\n") :]
    assert f"## {report.run_id}" in entry
    assert "- base: `" in entry
    assert "- upgrade: restamp" in entry
    assert "- restamped: 2" in entry
    assert "- tags-recovered: 0" in entry
    assert "- dispositions:" not in entry

    parsed = AgoraHandlers(repo)._recent_log()[0]
    assert parsed["run_id"] == report.run_id
    assert parsed["base"]
    assert parsed["ops"] == {}


def test_the_commit_subject_is_a_maintenance_verb(tmp_path: Path) -> None:
    """``curate:`` names an INGEST run; a history that cannot tell the two apart cannot answer
    "when did the wiki last change because the curator thought something"."""
    import subprocess

    repo = _converted_repo(tmp_path)
    source = build_vault_tag_index(_vault(tmp_path / "vault", **{"curator-concurrency": "infra"}))

    _restamp(repo, tag_source=source)

    subject = subprocess.run(  # noqa: S603
        ["git", "log", "-1", "--pretty=%s"],
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert subject.startswith("chore(kb): upgrade --restamp (")
    assert "curate:" not in subject
    assert "source_links=2" in subject
    assert "tags=1" in subject
    assert "taxonomy=+1" in subject


# --- (22) + the schema refusal --------------------------------------------------------------------
def test_a_malformed_stamped_note_refuses_the_run(tmp_path: Path) -> None:
    """A note carrying the curator's stamp whose fence is broken is an INTEGRITY signal, not a
    draft — the same discriminator ``worker.run`` applies, reached through the same scan."""
    repo = _converted_repo(tmp_path)
    (repo.root / "wiki" / "concepts" / "broken.md").write_text(
        "---\ntitle: broken\nsources: []\n", encoding="utf-8"
    )
    repo.commit_worktree(repo.root, "chore: break a stamped note", when=INIT_AT)
    tip = repo.branch_commit()

    report = _restamp(repo)

    assert report.status == "refused"
    assert any("LIVE-TREE" in r for r in report.reasons), report.reasons
    assert repo.branch_commit() == tip


def test_a_read_only_schema_1_repo_is_refused(tmp_path: Path) -> None:
    """Belt-and-braces behind the face's own ADR-0041 D6 refusal: this engine publishes a curated
    tree and will not do so into a schema this build does not write."""
    root = tmp_path / "kb"
    build_kb(root, _CORPUS[:2], schema_version=1)
    repo = Repo(RepoLayout(root))
    repo.init(when=INIT_AT, schema_version=1)
    tip = repo.branch_commit()

    report = _restamp(repo)

    assert report.status == "refused"
    assert any("SCHEMA" in r and "import --from-kb" in r for r in report.reasons), report.reasons
    assert repo.branch_commit() == tip


# --- the pure planner is pure ---------------------------------------------------------------------
def test_plan_restamp_writes_nothing(tmp_path: Path) -> None:
    repo = _converted_repo(tmp_path)
    before = _tree_bytes(repo)

    plan = plan_restamp(repo.layout, schema_version=2)

    assert plan.changed is True
    assert _tree_bytes(repo) == before


# --- human-authored notes: the #152 split, applied ------------------------------------------------
HUMAN_DRAFT = "wiki/concepts/human-draft.md"

#: A claim-bearing note carrying NEITHER of `worker.CURATOR_STAMP_KEYS` — `sources:` nor
#: `timestamp:` — which is what makes `is_curator_written` classify it as somebody's own draft. It
#: also omits four L1-4 required keys, so it is simultaneously the lint-scope case: an unfinished
#: draft must not be able to refuse a backfill of the notes around it.
_HUMAN_DRAFT_TEXT = (
    "---\n"
    "title: Human Draft\n"
    "kind: concept\n"
    f"kb: {FIXTURE_KB_ID}\n"
    "subjects:\n"
    "- ai-tech\n"
    "aliases: []\n"
    "tags:\n"
    "- mine\n"
    "status: stub\n"
    "---\n"
    "\n"
    "# Human Draft\n"
    "\n"
    "Something a person is still writing.\n"
)


def _repo_with_a_human_draft(tmp_path: Path) -> Repo:
    root = tmp_path / "kb"
    build_kb(root, _CORPUS, schema_version=2, kb_id=FIXTURE_KB_ID)
    (root / HUMAN_DRAFT).write_text(_HUMAN_DRAFT_TEXT, encoding="utf-8")
    repo = Repo(RepoLayout(root))
    repo.init(when=INIT_AT, schema_version=2, kb_id=FIXTURE_KB_ID)
    return repo


def test_a_note_with_no_curator_stamp_is_skipped_and_left_byte_identical(tmp_path: Path) -> None:
    """``is_curator_written``'s contract, honoured in both directions.

    A human's note in ``wiki/`` is "read, indexed, linkable — never graded, never written to". The
    tag leg REPLACES ``tags:``, so writing one would silently delete a hand-authored vocabulary; the
    mirror leg would re-seat a key the curator never minted. The note is reported as a skip instead.
    """
    repo = _repo_with_a_human_draft(tmp_path)
    before = (repo.root / HUMAN_DRAFT).read_bytes()
    source = _FakeTagSource({"human-draft": TagMatch("matched", tags=("agent", "infra"))})

    report = _restamp(repo, tag_source=source)

    assert report.status == "published"
    change = next(n for n in report.plan.notes if n.rel_path == HUMAN_DRAFT)
    assert change.skipped == restamp_mod.SKIP_HUMAN_AUTHORED
    assert change.changed is False
    assert (repo.root / HUMAN_DRAFT).read_bytes() == before
    assert HUMAN_DRAFT not in _changed_paths(repo, repo.branch_commit())
    # …and the run it did not belong to still did its job.
    assert _fm(repo, CONCEPT_A)[SOURCE_LINKS_KEY]


def test_a_lint_invalid_human_draft_cannot_refuse_the_whole_backfill(tmp_path: Path) -> None:
    """The #152 failure mode, closed. An untouched note never enters ``touched``, so it is outside
    this run's lint scope — exactly as ``lint()``'s own contract says a human's note must be, or one
    unfinished draft perpetually rejects a run that has no business grading it."""
    repo = _repo_with_a_human_draft(tmp_path)
    tip = repo.branch_commit()

    report = _restamp(repo)

    assert report.status == "published", report.reasons
    assert repo.branch_commit() != tip
    assert report.lint is not None
    assert not [f for f in report.lint.findings if f.path == HUMAN_DRAFT]


def test_a_human_note_contributes_no_tag_to_the_taxonomy(tmp_path: Path) -> None:
    """A skipped note's tags are not written, so declaring them would widen the repo's closed
    vocabulary for a use that never happens."""
    repo = _repo_with_a_human_draft(tmp_path)
    source = _FakeTagSource({"human-draft": TagMatch("matched", tags=("agent",))})

    report = _restamp(repo, tag_source=source)

    assert report.plan.taxonomy_added == ()
    doc = yaml.safe_load((repo.root / TAXONOMY_REL_PATH).read_text(encoding="utf-8"))
    assert doc["allowed_tags"] == {}


# --- the receipts count BYTES, not values ---------------------------------------------------------
def test_a_re_seated_mirror_is_counted_and_named(tmp_path: Path) -> None:
    """A mirror that is correct but sits away from ``sources:`` is REWRITTEN by APPLY's stamper.

    ``log.md`` and the commit are this run's only receipts, so a value-keyed counter would record
    ``restamped: 0`` for a commit that modified the note — a receipt smaller than its own diff.
    """
    repo = _converted_repo(tmp_path)
    _restamp(repo)  # mint the mirror adjacent to `sources:`
    text = (repo.root / CONCEPT_A).read_text(encoding="utf-8")
    fm, body = frontmatter.parse(text)
    moved = {k: v for k, v in fm.items() if k != SOURCE_LINKS_KEY}
    moved[SOURCE_LINKS_KEY] = fm[SOURCE_LINKS_KEY]  # …now at the END of the block
    (repo.root / CONCEPT_A).write_text(frontmatter.render(moved, body), encoding="utf-8")
    repo.commit_worktree(repo.root, "chore: move the mirror away from sources:", when=INIT_AT)

    report = _restamp(repo)

    assert report.status == "published"
    change = next(n for n in report.plan.notes if n.rel_path == CONCEPT_A)
    assert change.changed is True
    assert change.source_links_before == change.source_links_after  # no VALUE moved
    assert change.mirror_rewritten is True  # …but the bytes did
    assert report.plan.restamped == 1
    assert CONCEPT_A in _changed_paths(repo, repo.branch_commit())
    assert "- restamped: 1" in (repo.root / "log.md").read_text(encoding="utf-8")


def test_a_note_the_run_does_not_rewrite_is_counted_nowhere(tmp_path: Path) -> None:
    """The other half of the same predicate: no re-seat, no count, no diff."""
    repo = _converted_repo(tmp_path)
    _restamp(repo)

    report = _restamp(repo)

    assert report.status == "noop"
    assert report.plan.restamped == 0
    assert all(not n.mirror_rewritten for n in report.plan.notes)


# --- the preview sees every refusal the run does -------------------------------------------------
def test_a_dry_run_reports_the_taxonomy_policy_refusal(tmp_path: Path) -> None:
    """D8 is evaluated in the PURE planner, so a preview cannot print a clean plan over a run that
    refuses. The face renders ``plan.reasons`` as ``would refuse — …``; a gate reachable only after
    the lock would never reach it."""
    repo = _repo_with_policy(tmp_path / "kb", "review-only")
    source = build_vault_tag_index(_vault(tmp_path / "vault", **{"curator-concurrency": "infra"}))

    preview = _restamp(repo, tag_source=source, dry_run=True)
    real = _restamp(repo, tag_source=source)

    assert preview.status == "dry-run"
    assert any("TAXONOMY-POLICY" in r for r in preview.reasons), preview.reasons
    assert real.status == "refused"
    assert preview.reasons == real.reasons
    # A refused evolution writes no taxonomy text, so the run cannot half-land the widening.
    assert preview.plan.taxonomy_text is None


def test_a_dry_run_warns_when_the_working_copy_is_dirty(tmp_path: Path) -> None:
    """A dirty tree is invisible to the branch/HEAD comparison, and an untracked note is planned by
    the preview and unplannable by the real run, which mounts the curated ref."""
    repo = _converted_repo(tmp_path)
    (repo.root / "wiki" / "concepts" / "zzz-untracked.md").write_text(
        _HUMAN_DRAFT_TEXT, encoding="utf-8"
    )

    report = _restamp(repo, dry_run=True)

    assert report.status == "dry-run"
    assert any("uncommitted or untracked" in w for w in report.warnings), report.warnings
    assert any("zzz-untracked.md" in w for w in report.warnings), report.warnings


def test_a_clean_working_copy_draws_no_dirty_warning(tmp_path: Path) -> None:
    repo = _converted_repo(tmp_path)

    report = _restamp(repo, dry_run=True)

    assert not [w for w in report.warnings if "uncommitted or untracked" in w]


# --- the taxonomy file is RE-RENDERED, and says so ------------------------------------------------
def test_a_hand_commented_taxonomy_is_reported_as_re_rendered(tmp_path: Path) -> None:
    """``_meta/taxonomy.yaml`` is human-written and this path re-renders it through the ONE emitter
    (D7), so comments and hand layout do not survive the widening. The loss is cosmetic — values and
    per-tag descriptors are preserved — but it is not silent."""
    root = tmp_path / "kb"
    build_kb(root, _CORPUS, schema_version=2, kb_id=FIXTURE_KB_ID)
    (root / TAXONOMY_REL_PATH).write_text(
        "schema_version: 2\n"
        "taxonomy_policy: open\n"
        "domains:\n"
        "- ai-tech        # the main domain\n"
        "- general\n"
        "# The closed vocabulary. Add a tag ONLY after a review.\n"
        "allowed_tags: {}\n",
        encoding="utf-8",
    )
    repo = Repo(RepoLayout(root))
    repo.init(when=INIT_AT, schema_version=2, kb_id=FIXTURE_KB_ID)
    source = _FakeTagSource({"curator-concurrency": TagMatch("matched", tags=("infra",))})

    report = _restamp(repo, tag_source=source)

    assert report.status == "published"
    assert report.plan.taxonomy_reformatted is True
    written = (repo.root / TAXONOMY_REL_PATH).read_text(encoding="utf-8")
    assert "#" not in written  # the honest half of the report: they really are gone
    assert yaml.safe_load(written)["allowed_tags"] == {"infra": {}}


def test_a_taxonomy_this_build_already_renders_is_not_reported_as_re_rendered(
    tmp_path: Path,
) -> None:
    """The common case — a repo-init file — must not cry wolf, or the line stops being read."""
    repo = _converted_repo(tmp_path)
    source = _FakeTagSource({"curator-concurrency": TagMatch("matched", tags=("infra",))})

    report = _restamp(repo, tag_source=source)

    assert report.plan.taxonomy_added == ("infra",)
    assert report.plan.taxonomy_reformatted is False
