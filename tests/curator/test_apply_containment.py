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

**KB wiki schema 2 (ADR-0041) narrows the attack surface and this suite records where.** Two of the
three tokens that used to compose a wiki path no longer do: a note basename now passes through
:meth:`~agora_kb.core.layout.RepoLayout.note_path_for`\'s validator (the D4.4 pathsafe component
check plus the reserved leading-``_`` rejection) BEFORE any path is joined, and the journal path is
composed entirely from the curator-owned ``run_date`` so a model-supplied ``basename`` reaches no
filesystem call at all (D2.6). What is left for ``_contained`` to catch is the residue that no
charset rule can judge — a symlinked kind directory, and the ``raw/`` shard/event tokens that come
from PROVENANCE rather than from the plan — and those are the cases below that still assert on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.config import KbIdentity, write_kb_identity
from agora_kb.core.layout import RepoLayout
from agora_kb.curator.apply import ApplyError, _contained, apply_plan
from agora_kb.curator.plan import Disposition, Plan
from agora_kb.schema.emit import Taxonomy, emit_schema

RUN_ID = "2026-09-03T03-00-00.000Z--7f31ab"
RUN_DATE = "2026-09-03"
E1 = "2026-09-03T02-40-10.000Z--a1b2c3"

KB_ID = "01J8ZQ3M4N5P6Q7R8S9T0V1W2X"

TAXONOMY = Taxonomy(
    schema_version=2,
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
    # APPLY writes KB wiki schema 2 and refuses a repo with no identity to stamp into `kb:`
    # (ADR-0041 D1.5) — without this the containment assertions below would all pass for the WRONG
    # reason, on an error raised before any path was ever composed.
    write_kb_identity(layout, KbIdentity(kb_id=KB_ID, name="agora-test", declared_kind="personal"))
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


@pytest.mark.parametrize(
    "basename_kind",
    [
        "deep-relative",  # ../../../../tmp/agora-escape — the §12.4 named case
        # Four levels is what it TAKES: the note sits at repo/wiki/concepts/, so "../x" only climbs
        # inside the tree. The escape budget is a property of the layout, and this case lands
        # squarely in tmp_path where the assertions below can see it.
        "relative",  # ../../../../agora-escape
        "absolute",  # absolute path — pathlib DISCARDS the worktree operand entirely
    ],
)
def test_escaping_basename_raises_and_writes_nothing_outside(
    tmp_path: Path, basename_kind: str
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

    # Under schema 2 this is refused one step EARLIER than it used to be — by the pathsafe
    # component check inside `note_path_for` (ADR-0041 D4.4), before any path is joined — and the
    # error is still the same PATH/ALLOWLIST class, because `_note_path` translates the composer\'s
    # ValueError into ApplyError rather than letting it escape as the uncaught traceback ADR-0011
    # §4 forbids. `_contained` remains the backstop for the tokens a charset rule cannot judge.
    plan = _plan(_disp(basename=basename))
    with pytest.raises(ApplyError, match="PATH/ALLOWLIST"):
        apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))

    # Nothing new anywhere outside the worktree — not the escape target, not a parent directory
    # created on the way there by mkdir(parents=True).
    assert _outside(tmp_path, wt) == before
    assert not (tmp_path / "agora-escape.md").exists()


@pytest.mark.parametrize("basename", ["../../../../agora-escape", "_blob", "2026-09-04"])
def test_append_daily_basename_is_never_a_path_token(tmp_path: Path, basename: str) -> None:
    # ADR-0041 D2.6: the journal path is composed ENTIRELY from the injected `run_date` — both the
    # `<yyyy>/<mm>` shard and the basename — so a model-supplied `basename` is not a path input at
    # all any more. That is a stronger property than "the escaping one is rejected": an escaping
    # basename, a RESERVED one (`_blob`), and a merely WRONG-BUT-VALID one (another date) all land
    # the journal at exactly the same canonical path, and none of them reaches a filesystem call.
    # Parsing the shard back out of a model-supplied basename is the inversion D2.6 forbids.
    wt = _worktree(tmp_path)
    before = _outside(tmp_path, wt)

    plan = _plan(_disp(op="APPEND_DAILY", basename=basename))
    apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))

    journal = wt / "wiki" / "notes" / RUN_DATE[:4] / RUN_DATE[5:7] / f"{RUN_DATE}.md"
    assert journal.is_file()
    assert _outside(tmp_path, wt) == before
    assert not (tmp_path / "agora-escape.md").exists()


def test_escaping_domain_raises_and_writes_nothing_outside(tmp_path: Path) -> None:
    # The domain token has left the WIKI path entirely under schema 2 (ADR-0041 D2.2) — it survives
    # in exactly one place, as the `raw/<domain>/<event_id>.md` SHARD KEY (leg 3), because `raw/`
    # never moves. So it is still the mkdir(parents=True) vector, just for a different tree: an
    # escape here creates directories outside the repo even if the final write then fails. A body
    # on the provenance tuple is what makes the engine actually materialize that raw/ file.
    wt = _worktree(tmp_path)
    before = _outside(tmp_path, wt)

    plan = _plan(_disp(domain="../../escaped-domain"))
    with pytest.raises(ApplyError, match="PATH/ALLOWLIST"):
        apply_plan(
            plan,
            worktree=wt,
            run_date=RUN_DATE,
            provenance=_provenance("c1", E1, body="planted body"),
        )

    assert _outside(tmp_path, wt) == before
    assert not (tmp_path / "escaped-domain").exists()


def test_reserved_underscore_domain_cannot_compose_a_map(tmp_path: Path) -> None:
    # ADR-0041 D1.4/D4.4 layer 1: `raw/<domain>/` and `raw/_blob/` share ONE namespace, and the
    # pathsafe swap REMOVES the leading-`_` rejection the old ASCII token regex gave for free. A
    # subject named `_blob` would otherwise compose `wiki/maps/_blob.md` here — and the same class
    # of token composes into `raw/`. The composer refuses it, and no map is left behind.
    wt = _worktree(tmp_path)
    plan = _plan(_disp(domain="_blob"))
    with pytest.raises(ApplyError, match="PATH/ALLOWLIST"):
        apply_plan(plan, worktree=wt, run_date=RUN_DATE, provenance=_provenance("c1", E1))
    assert not (wt / "wiki" / "maps" / "_blob.md").exists()


def test_symlinked_kind_directory_is_refused(tmp_path: Path) -> None:
    # Regex-clean tokens end-to-end; the escape is an inode, planted in the worktree before the run
    # (a prior run, a harvested repo, a hostile clone). A character rule cannot reach this case at
    # all, which is exactly the claim plan.py's comment used to make and no longer does. Under
    # schema 2 the vulnerable directory is the KIND directory: the subject no longer names one.
    wt = _worktree(tmp_path)
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    (wt / "wiki").mkdir(exist_ok=True)
    (wt / "wiki" / "concepts").symlink_to(outside_dir, target_is_directory=True)

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
    """A worktree holding two real concepts, so a WIDENED lookup would have something to hit."""
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
    alpha = wt / "wiki" / "concepts" / "alpha-note.md"
    beta = wt / "wiki" / "concepts" / "beta-note.md"
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
    alpha = wt / "wiki" / "concepts" / "alpha-note.md"
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
