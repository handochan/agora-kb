"""Worktree containment at the APPLY write sites (Stratum UNIT 1, S2).

Every path :mod:`agora_kb.curator.apply` writes is composed from plan-supplied tokens, and until
``_contained`` existed the only thing keeping those tokens inside the repo was the §4.1
PATH/ALLOWLIST safe-token regex in :mod:`agora_kb.curator.plan` — a gate in a *different* module,
one call up. These tests deliberately BYPASS that regex (they construct :class:`Disposition` and the
provenance tuples directly, exactly as a future caller, a widened charset, or a refactor could) and
assert that APPLY itself refuses: it raises :class:`ApplyError` and leaves ZERO bytes outside the
worktree.

The suite runs while ``plan.py``'s token pattern is still the strict ASCII regex, on purpose: the
containment property is proved green under the OLD charset, so if a later slug change breaks
something, the failure is unambiguously the slug change and not this gate.

Three escape classes are covered — a ``..`` traversal, an absolute path (``worktree / "/tmp/x"``
discards the left operand, which a purely textual check would miss), and a symlinked directory (a
name a character rule cannot possibly judge) — plus the glob-widening variant of the same bug in
``_resolve_target_path``, where a metacharacter in ``target_basename`` used to retarget the write
onto an unrelated note rather than escape the tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.core.layout import RepoLayout
from agora_kb.curator.apply import ApplyError, _contained, apply_plan
from agora_kb.curator.plan import Disposition, Plan
from agora_kb.schema.emit import Taxonomy, emit_schema

RUN_ID = "2026-09-03T03-00-00.000Z--7f31ab"
RUN_DATE = "2026-09-03"
E1 = "2026-09-03T02-40-10.000Z--a1b2c3"

TAXONOMY = Taxonomy(
    schema_version=1,
    taxonomy_policy="open",
    allowed_tags=("curator", "concurrency", "architecture"),
    domains=("ai-tech", "economy", "general"),
)


# --- fixtures -----------------------------------------------------------------------------------


def _worktree(tmp_path: Path) -> Path:
    """A repo worktree nested INSIDE ``tmp_path`` so a one-level escape is observable.

    The worktree is ``tmp_path/repo``, never ``tmp_path`` itself: an escaping write then lands in
    ``tmp_path`` — a directory this test owns and can assert about — instead of somewhere in the
    real filesystem that a test must not create files in (or, worse, that already has a file there
    and makes the assertion vacuous).
    """
    root = tmp_path / "repo"
    root.mkdir()
    layout = RepoLayout(root)
    emit_schema(layout, taxonomy=TAXONOMY)
    return root


def _outside(tmp_path: Path, worktree: Path) -> set[Path]:
    """Every path under ``tmp_path`` that is NOT inside ``worktree`` (the escape observatory)."""
    return {p for p in tmp_path.rglob("*") if not p.is_relative_to(worktree)}


def _provenance(candidate_id: str, *event_ids: str, body: str | None = None) -> dict:
    return {
        candidate_id: [
            {
                "event_id": e,
                "source": "claude-code",
                "writer": "dochan",
                "cwd": "/tmp/psa",
                "raw_ref": None,
                "created": "2026-09-03T02-40-10.000Z",
                **({"body": body} if body is not None else {}),
            }
            for e in event_ids
        ]
    }


def _plan(*dispositions: Disposition) -> Plan:
    return Plan(schema_version=1, run_id=RUN_ID, finished=True, dispositions=tuple(dispositions))


def _disp(**overrides: object) -> Disposition:
    """A CREATE_THEME disposition built DIRECTLY — the §4.1 plan gate is deliberately not run."""
    base: dict[str, object] = {
        "candidate_id": "c1",
        "event_ids": (E1,),
        "op": "CREATE_THEME",
        "domain": "ai-tech",
        "basename": "curator-concurrency",
        "title": "Curator concurrency model",
        "summary": "One curator advances the curated branch under a per-repo lock.",
        "status": "active",
        "tags": ("curator",),
        "aliases": (),
        "links": (),
        "needs_prose": True,
        "reason": "New concept.",
    }
    base.update(overrides)
    return Disposition(**base)


# --- the helper itself --------------------------------------------------------------------------


def test_contained_returns_the_original_path_object_unmodified(tmp_path: Path) -> None:
    # Containment is ASSERTED, never REPAIRED, and never re-spelled: callers must keep writing to
    # the exact path they composed, or a worktree reached through a symlinked ancestor would change
    # the bytes' destination and break the `relative_to(worktree)` derivations in _apply_merge.
    #
    # This has to go through an actually-symlinked ANCESTOR (not just a not-yet-resolved worktree
    # root) to be a real assertion: on a host where tmp_path itself sits under a symlink (macOS's
    # /var -> /private/var), `candidate` built from an unresolved `tmp_path` is already identical to
    # its own `resolve()`, so returning the RESOLVED twin would pass this test vacuously — and did,
    # silently, until this case was added; it reproduces the exact ValueError this unit fixed in
    # `_apply_merge` (`resolved-plan-path.relative_to(unresolved-worktree)`), just one call earlier.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    candidate = link / "wiki" / "d" / "themes" / "n.md"  # does not exist yet
    assert _contained(link, candidate) == candidate
    assert _contained(link, candidate) != real / "wiki" / "d" / "themes" / "n.md"


def test_contained_allows_dot_dot_inside_a_component(tmp_path: Path) -> None:
    # "a..b" contains ".." but is ONE component and escapes nothing. A substring denylist would
    # reject it; the resolve()-based check must not — this pins that the gate is not textual.
    candidate = tmp_path / "wiki" / "d" / "themes" / "a..b.md"
    assert _contained(tmp_path, candidate) == candidate


@pytest.mark.parametrize(
    "candidate_parts",
    [
        ("..", "escape.md"),
        ("wiki", "..", "..", "escape.md"),
        ("/etc", "passwd"),  # absolute: pathlib DISCARDS the worktree operand entirely
    ],
)
def test_contained_rejects_every_escape_shape(tmp_path: Path, candidate_parts: tuple) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    candidate = root
    for part in candidate_parts:
        candidate = candidate / part
    with pytest.raises(ApplyError, match="PATH/ALLOWLIST"):
        _contained(root, candidate)


def test_contained_rejects_a_symlinked_directory_pointing_out(tmp_path: Path) -> None:
    # The escape a NAME regex can never see: every component is regex-clean, but one of them is a
    # symlink whose target is outside. resolve() follows it; is_relative_to then says no.
    root = tmp_path / "repo"
    (root / "wiki").mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (root / "wiki" / "ai-tech").symlink_to(target, target_is_directory=True)
    with pytest.raises(ApplyError, match="PATH/ALLOWLIST"):
        _contained(root, root / "wiki" / "ai-tech" / "themes" / "n.md")


# --- CREATE_THEME / APPEND_DAILY: the plan gate bypassed --------------------------------------


@pytest.mark.parametrize("op", ["CREATE_THEME", "APPEND_DAILY"])
@pytest.mark.parametrize(
    "basename_kind",
    [
        "deep-relative",  # ../../../../tmp/agora-escape — the §12.4 named case
        # Four levels is what it TAKES: the note sits at repo/wiki/<domain>/themes/, so "../x" only
        # climbs to the domain directory and stays (wrongly, but harmlessly) inside the tree. The
        # escape budget is a property of the layout, and this case lands squarely in tmp_path where
        # the assertions below can see it.
        "relative",  # ../../../../agora-escape
        "absolute",  # absolute path — pathlib DISCARDS the worktree operand entirely
    ],
)
def test_escaping_basename_raises_and_writes_nothing_outside(
    tmp_path: Path, op: str, basename_kind: str
) -> None:
    wt = _worktree(tmp_path)
    before = _outside(tmp_path, wt)

    # Built from ``tmp_path`` rather than hard-coded (e.g. "/tmp/agora-escape") so an ABSOLUTE
    # escape, if the containment gate ever regressed, would land where ``_outside`` can see it and
    # this test can clean it up — not silently write into the host's real /tmp.
    basename = {
        "deep-relative": "../../../../tmp/agora-escape",
        "relative": "../../../../agora-escape",
        "absolute": str(tmp_path / "agora-escape"),
    }[basename_kind]

    plan = _plan(_disp(op=op, basename=basename))
    with pytest.raises(ApplyError, match="PATH/ALLOWLIST"):
        apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))

    # Nothing new anywhere outside the worktree — not the escape target, not a parent directory
    # created on the way there by mkdir(parents=True).
    assert _outside(tmp_path, wt) == before
    assert not (tmp_path / "agora-escape.md").exists()


def test_escaping_domain_raises_and_writes_nothing_outside(tmp_path: Path) -> None:
    # The domain token composes the DIRECTORY half of the path, so it is the mkdir(parents=True)
    # vector specifically: an escape here creates directories outside the repo even if the final
    # write then fails.
    wt = _worktree(tmp_path)
    before = _outside(tmp_path, wt)

    plan = _plan(_disp(domain="../../escaped-domain"))
    with pytest.raises(ApplyError, match="PATH/ALLOWLIST"):
        apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))

    assert _outside(tmp_path, wt) == before
    assert not (tmp_path / "escaped-domain").exists()


def test_symlinked_domain_directory_is_refused(tmp_path: Path) -> None:
    # Regex-clean tokens end-to-end; the escape is an inode, planted in the worktree before the run
    # (a prior run, a harvested repo, a hostile clone). A character rule cannot reach this case at
    # all, which is exactly the claim plan.py's comment used to make and no longer does.
    wt = _worktree(tmp_path)
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    (wt / "wiki").mkdir(exist_ok=True)
    (wt / "wiki" / "ai-tech").symlink_to(outside_dir, target_is_directory=True)

    plan = _plan(_disp())
    with pytest.raises(ApplyError, match="PATH/ALLOWLIST"):
        apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))

    assert list(outside_dir.rglob("*")) == []


# --- raw/ provenance: the token that never sees the plan regex at all ---------------------------


def test_escaping_event_id_in_provenance_raises_before_probing_the_filesystem(
    tmp_path: Path,
) -> None:
    # The canonical raw/ ref is composed as ``raw/<domain>/<event_id>.md`` from the PROVENANCE
    # tuple, which the §4.1 plan gate never graded — it grades the plan, and provenance is threaded
    # in beside it by the worker. So ``event_id`` is a path token with no charset gate anywhere, and
    # it is the one site that READS before it writes (the immutable re-cite branch): containment has
    # to come first, or a failed run still discloses whether an out-of-tree file exists.
    #
    # (An EXPLICIT ``raw_ref`` on the tuple is cite-only today — _sources_union puts it in
    # ``sources:`` and never materializes it — so it reaches no filesystem call in this module. If a
    # future change starts materializing it, it goes through the same helper.)
    wt = _worktree(tmp_path)
    before = _outside(tmp_path, wt)
    provenance = {
        "c1": [
            {
                "event_id": "../../../agora-escape-raw",
                "source": "claude-code",
                "writer": "dochan",
                "cwd": "/tmp/psa",
                "raw_ref": None,
                "created": "2026-09-03T02-40-10.000Z",
                "body": "planted body",
            }
        ]
    }

    plan = _plan(_disp())
    with pytest.raises(ApplyError, match="PATH/ALLOWLIST"):
        apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=provenance)

    assert _outside(tmp_path, wt) == before
    assert not (tmp_path / "agora-escape-raw.md").exists()


# --- _resolve_target_path: the glob-widening twin ------------------------------------------------


def _seed_two_themes(tmp_path: Path) -> Path:
    """A worktree holding two real themes, so a WIDENED lookup would have something to hit."""
    wt = _worktree(tmp_path)
    apply_plan(
        _plan(
            _disp(candidate_id="c1", basename="alpha-note"),
            _disp(candidate_id="c2", basename="beta-note", title="Beta"),
        ),
        worktree=wt,
        run_date=RUN_DATE,
        provenance={**_provenance("c1", E1), **_provenance("c2", E1)},
    )
    return wt


@pytest.mark.parametrize("target", ["*", "?lpha-note", "[ab]*", "alpha-not?"])
def test_glob_metacharacters_in_target_basename_cannot_widen_the_search(
    tmp_path: Path, target: str
) -> None:
    # ``target_basename`` used to be interpolated into an rglob PATTERN. "*" then matched every note
    # in the tree and the first sorted match won, so a crafted target silently RETARGETED the merge
    # onto an unrelated note — a wrong-file write, not an escape, and invisible to any containment
    # check. Exact-name matching makes all four of these simply not found.
    wt = _seed_two_themes(tmp_path)
    alpha = wt / "wiki" / "ai-tech" / "themes" / "alpha-note.md"
    beta = wt / "wiki" / "ai-tech" / "themes" / "beta-note.md"
    before = (alpha.read_bytes(), beta.read_bytes())

    plan = _plan(
        _disp(
            candidate_id="c3",
            op="MERGE_INTO_THEME",
            basename=None,
            target_basename=target,
            needs_prose=True,
        )
    )
    with pytest.raises(ApplyError, match="not found"):
        apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c3", E1))

    assert (alpha.read_bytes(), beta.read_bytes()) == before


def test_traversing_target_basename_is_not_found_and_touches_nothing(tmp_path: Path) -> None:
    wt = _seed_two_themes(tmp_path)
    outside = tmp_path / "outside-note.md"
    outside.write_text("untouched\n", encoding="utf-8")

    plan = _plan(
        _disp(
            candidate_id="c3",
            op="MERGE_INTO_THEME",
            basename=None,
            target_basename="../../../outside-note",
            needs_prose=True,
        )
    )
    with pytest.raises(ApplyError, match="not found"):
        apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c3", E1))

    assert outside.read_text(encoding="utf-8") == "untouched\n"


def test_an_exact_target_basename_still_resolves(tmp_path: Path) -> None:
    # The negative tests above would all pass on a lookup that found NOTHING ever. This is the
    # positive control: exact-name matching still resolves a real target and merges into it.
    wt = _seed_two_themes(tmp_path)
    alpha = wt / "wiki" / "ai-tech" / "themes" / "alpha-note.md"
    before = alpha.read_bytes()

    plan = _plan(
        _disp(
            candidate_id="c3",
            op="MERGE_INTO_THEME",
            basename=None,
            target_basename="alpha-note",
            needs_prose=True,
        )
    )
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c3", E1))
    assert alpha.read_bytes() != before
