"""Layout-aware builder for a COMPLETE, LINT-CLEAN synthetic knowledge repo.

Why this exists (UNIT 3 of the Stratum plan, gate B). The ranking golden fixture has to pin what
``Wiki.query`` returns TODAY, *before* the wiki layout axis flips from v1
(``wiki/<domain>/themes|daily`` + ``wiki/<domain>/<domain>-moc.md``) to the Stratum kind-first
layout. ``core.wiki._is_moc_path`` recognizes a MOC purely from its PATH, and every MOC seeds
``d_moc = 0`` for its children — so the layout flip moves the structural term for the whole corpus.
Without one corpus that can be materialized under EITHER layout from the SAME note content, a
ranking delta could never be attributed to the flip rather than to the fixture.

Hence the split this module is built around:

* **CONTENT** is a list of :class:`NoteSpec` — title, body, frontmatter, links. It knows nothing
  about directories.
* **LAYOUT** is a private layout object selected by ``schema_version`` — it decides paths, note
  basenames and the relative link targets that map/index bullets carry.

Both layouts are now implemented (UNIT 2 / wave W2.1). ``schema_version=1`` is the ADR-0010 v1
layout, **byte-identical to what UNIT 3 recorded the pre-flip golden over**; ``schema_version=2`` is
the ADR-0041 kind-first layout. :mod:`tests.rank_golden.corpus` was NOT touched by the flip — no
title, body, tag, alias or map label moved — which is the property that makes a ranking delta
attributable to the layout rather than to the fixture.

**What the flip does to the corpus, stated rather than discovered.** Three things move, and only
three:

1. **paths** — ``wiki/<domain>/themes/<slug>.md`` becomes ``wiki/concepts/<slug>.md`` and the
   subject moves into ``subjects:`` (ADR-0041 D1/D2.2);
2. **the map basename** — ``<domain>-moc`` becomes ``<domain>`` (``wiki/maps/<domain>.md``): the
   kind marker left the filename for the directory, which is the whole point of D5;
3. **the journal identity** — v1's one daily *per domain per run* becomes ADR-0041 D2.6's ONE
   journal per ``run_date``, repo-wide, basenamed ``<yyyy>-<mm>-<dd>``. Same-dated dailies from
   different domains are MERGED into that one note (D6 step 4): one ``## <contributor title>``
   section each in domain order, ``sources``/``tags``/``aliases``/``subjects`` unioned, and the
   remaining scalars taken from the first contributor in domain order.

Everything else — every concept/summary/entity basename, every body, every alias — is carried over
unchanged, which is what :func:`v2_basename` lets a test assert instead of assume.

What "complete" means here is exactly what the real L1 linter
(:func:`agora_kb.schema.lint.lint`) demands of a repo **on that schema** (it dispatches on the
emitted ``_meta/taxonomy.yaml`` ``schema_version``), so the fixture can never drift into a shape the
curator would reject:

* the real schema emitter (:func:`agora_kb.schema.emit.emit_schema`) writes ``AGENTS.md`` (+ its
  agent-guide symlinks), ``_meta/taxonomy.yaml`` and ``_templates/`` — the same admin/init path
  ``agora repo init`` runs, so the schema-doc header and the taxonomy agree (L1-17);
* the taxonomy's ``allowed_tags`` is the union of every tag the specs use and its ``domains`` the
  set of domains, so tags/domains are in-vocabulary by construction (L1-5);
* every non-stub theme/concept (and, on schema 2, summary) gets non-empty ``sources:`` and the
  ``raw/<domain>/<event>.md`` evidence files those sources name are materialized (L1-7 / L1-8) —
  ``raw/`` is byte-identical across the two layouts, because ADR-0041 D1.4 never moves it;
* each map's ``children:`` is generated from the SAME list as its body child bullets, so the two
  sides cannot disagree (L1-6); likewise the root ``index.md`` over the maps. On schema 2 those
  children are additionally kept inside D1.3's admitted kind set, which L1-24 enforces;
* on schema 2, ``_meta/kb.yaml`` is written through the production writer
  (:func:`agora_kb.config.write_kb_identity`) with a FIXED fixture ``kb_id``, and every note mirrors
  it into ``kb:`` (ADR-0041 D1.5/D2) — a real ULID, minted nowhere, so two builds agree;
* ``wiki/summaries/``, ``wiki/entities/`` and ``wiki/people/`` are CREATED and left EMPTY unless a
  spec asks for one (ADR-0041 OD-7/OD-8): the containers exist, the populations do not.

``git init`` is deliberately NOT run: nothing in ``lint()`` or ``Wiki.query`` needs it (the ADR-0012
§2 reader cache simply reports "no curated commit" and the read path full-scans, which is the
oracle), and keeping the fixture git-free keeps it fast and free of git-config ambience.

Determinism: no wall clock, no locale, no randomness, no filesystem-order dependence. Dates come
from :data:`BUILDER_DATE` (or the spec), ordering follows the caller's spec order with domains
sorted, and every file is written UTF-8/LF/no-BOM (L1-16) with ``newline="\\n"`` so a Windows host
produces byte-identical bytes.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# The PRODUCTION slugger and the PRODUCTION canonical hash, imported rather than re-implemented.
# A private-looking `_slugify` is reached into on purpose: a second slugger in the fixture would
# drift from the one the curator/import paths actually use (#57 — the real one caps at 60 chars and
# rejects a slug that fails `_SLUG_OK_RE`), and a fixture that claims to carry the "#57 shape" while
# computing a different basename pins nothing about it.
from agora_kb.adapters.ollama_brain import _slugify as _production_slugify
from agora_kb.config import KbIdentity, write_kb_identity
from agora_kb.core.frontmatter import render
from agora_kb.core.hashing import content_sha256
from agora_kb.core.layout import KIND_DIRECTORIES, RepoLayout
from agora_kb.schema.emit import Taxonomy, emit_schema

__all__ = [
    "BUILDER_DATE",
    "FIXTURE_KB_ID",
    "FIXTURE_KB_NAME",
    "V1_KIND_TO_V2_KIND",
    "NoteKind",
    "NoteSpec",
    "build_kb",
    "hash_basename",
    "slugify",
    "v2_basename",
]

#: A spec's declared kind. The first four are the ADR-0010 v1 ``type:`` values and are the only ones
#: a schema-1 build accepts; ``summary`` / ``entity`` / ``person`` are the schema-2 kinds ADR-0041
#: adds (D2.5) and exist here so a fixture CAN populate those trees — by default it does not, since
#: OD-7/OD-8 ship ``wiki/summaries/`` and ``wiki/entities/`` empty.
NoteKind = Literal["theme", "daily", "moc", "index", "summary", "entity", "person"]

#: ADR-0041 D2.5's frozen ``type:`` → ``kind:`` table, extended with the three kinds that have no v1
#: antecedent and therefore map to themselves. This is the ONLY place the two vocabularies meet.
V1_KIND_TO_V2_KIND: dict[NoteKind, str] = {
    "theme": "concept",
    "daily": "note",
    "moc": "map",
    "index": "index",
    "summary": "summary",
    "entity": "entity",
    "person": "person",
}

#: The ``_meta/kb.yaml`` ``kb_id`` every schema-2 fixture stamps into each note's ``kb:``.
#:
#: A real, canonical ULID (it passes :func:`agora_kb.core.ids.is_ulid`, which
#: :class:`~agora_kb.config.KbIdentity` enforces) but a FIXED one: a fixture must mint nothing, or
#: two builds of the same corpus would differ in ``_meta/kb.yaml`` and in every note's frontmatter,
#: and the golden could never be recorded twice. Callers who need a different identity pass
#: ``kb_id=``.
FIXTURE_KB_ID = "01J8ZQ3M4N5P6Q7R8S9T0V1W2X"

#: The ``_meta/kb.yaml`` display name. Constant for the same reason ``FIXTURE_KB_ID`` is: deriving
#: it from ``root.name`` would make two builds at different temp paths produce different bytes.
FIXTURE_KB_NAME = "agora-fixture"

#: Frozen ``created``/``updated`` for every generated note. A fixture must read no wall clock
#: (ADR-0010 D1): a real date would make the corpus non-reproducible and, once a ``run_date`` is
#: supplied to ``lint()``, would eventually trip the L1-12 no-future check.
BUILDER_DATE = "2026-01-15"

_DAILY_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


def slugify(text: str) -> str:
    """Lowercase ASCII kebab slug — literally the slugger the CURATOR write path uses (#57).

    A thin re-export of :func:`agora_kb.adapters.ollama_brain._slugify` (collapse, trim, cap at 60,
    reject anything ``_SLUG_OK_RE`` would not accept), NOT a look-alike: if the production slugger
    ever changes, this fixture changes with it and the golden goes red, which is the only way the
    "#57 shape" claim in :mod:`tests.rank_golden.corpus` can mean anything. The IMPORT path's
    :func:`agora_kb.ingest.vault_import._slugify` is a looser variant (no 60-char cap, no
    ``_SLUG_OK_RE`` gate); the two agree only on the un-slugifiable case this fixture exercises.

    Returns ``""`` for text with no usable ASCII material (e.g. a purely Korean title); callers
    fall back to :func:`hash_basename`, exactly as the real write path does.
    """
    return _production_slugify(text)


def hash_basename(body: str) -> str:
    """``note-<sha8>`` basename for an un-slugifiable note (#57 fallback), over the note BODY.

    Same construction as :func:`agora_kb.adapters.ollama_brain._hash_fallback_basename`: the first
    8 hex chars of the DATA-MODEL §11.2 canonical ``content_sha256`` of the note's body — NFC,
    LF, per-line rstrip, one trailing newline — not a raw hash of the title. Passing the title
    here would be a different function wearing the same name.
    """
    return f"note-{content_sha256(body)[:8]}"


@dataclass
class NoteSpec:
    """One note's CONTENT — deliberately free of any path/layout decision.

    ``kind`` selects the ADR-0010 note type. ``theme`` and ``daily`` specs become note files;
    ``moc`` and ``index`` specs are OVERRIDES that supply the title/summary/tags/prose (and, for a
    ``moc``, the explicit ``children`` list) of the navigation note the builder generates — their
    child BULLETS are always generated from the same list as their ``children:`` frontmatter, so
    L1-6 holds by construction.

    ``slug`` is the note basename without ``.md``; when omitted it is :func:`slugify` of the title
    with the :func:`hash_basename` fallback (so a Korean-titled note lands on ``note-<sha8>``, the
    #57 shape #146 is about). For a ``daily`` the basename must be ``<domain>-YYYY-MM-DD``: pass it
    as ``slug`` or put the date in ``extra_frontmatter['date']``.

    ``moc_label`` is the LINK TEXT the domain MOC's bullet uses for this theme; it defaults to the
    title. It exists because ADR-0010 §3.2's child-bullet grammar captures the child from the link
    TARGET and leaves the label free, and ``core.wiki`` folds that label into ``moc_label_tokens`` —
    a set that is NOT one of the note's own scoring fields. A label equal to the title makes
    ``moc_label_tokens`` a subset of the title tokens, which means ``_passes_gate``'s ``d_moc == 0``
    branch can never be the sole reason a candidate is admitted, and the whole #146 husk class
    (``lex == 0``, admitted on the label alone) becomes unreachable. One theme in the corpus
    therefore carries a label sharing no token with any of its own fields.

    ``sources`` are repo-relative ``raw/`` paths; the builder materializes any that do not exist,
    and gives a non-stub theme a default one (L1-7/L1-8). ``related``/``children`` accept either a
    bare basename or a ``[[basename]]`` token — both are normalized to the ``[[basename]]`` form
    the frontmatter arrays keep (ADR-0014 D3).

    ``person`` is the ``wiki/people/<person>/`` namespace segment, REQUIRED for (and only read by) a
    ``kind='person'`` spec — the human-owned tree of ADR-0041 D3.3, which a schema-1 build has no
    home for at all. ``extra_frontmatter`` is the escape hatch for every schema-2 key the builder
    fills in with a default: passing ``{'provenance': {...}}`` or ``{'derived': True}`` overrides
    the default IN PLACE, keeping the ADR's documented key order.
    """

    kind: NoteKind
    domain: str
    title: str
    body: str
    slug: str | None = None
    moc_label: str | None = None
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    related: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    status: str = "active"
    person: str | None = None
    extra_frontmatter: dict[str, object] = field(default_factory=dict)

    def basename(self) -> str:
        """The note's basename in the **v1** layout (ADR-0010 D5).

        Layout-dependent for exactly two kinds, which is why :func:`v2_basename` exists rather than
        this method growing a ``schema_version`` argument: a ``moc`` is ``<domain>-moc`` here and
        ``<domain>`` under ADR-0041 D5, and a ``daily`` is ``<domain>-YYYY-MM-DD`` here while
        schema 2 merges every same-dated daily into one journal basenamed ``YYYY-MM-DD`` (D2.6).
        Every other kind's basename is the same under both layouts.
        """
        if self.slug:
            return self.slug
        if self.kind == "index":
            return "index"
        if self.kind == "moc":
            return f"{self.domain}-moc"
        if self.kind == "daily":
            date = self.extra_frontmatter.get("date")
            if not isinstance(date, str) or not _DAILY_DATE_RE.match(date):
                raise ValueError(
                    f"daily {self.title!r}: needs slug '<domain>-YYYY-MM-DD' or a "
                    f"'date' in extra_frontmatter"
                )
            return f"{self.domain}-{date}"
        # The #57 fallback, exercised for real: an un-slugifiable (e.g. purely Korean) title lands
        # on `note-<sha8>` of the note's own rendered body, the way the curator names it.
        return slugify(self.title) or hash_basename(_body_with_h1(self.title, self.body))


def v2_basename(spec: NoteSpec, *, date: str | None = None) -> str:
    """The basename ``spec`` gets under the **schema-2** layout (ADR-0041).

    Identical to :meth:`NoteSpec.basename` for every kind except the two the flip renames:

    * ``moc`` → ``<domain>`` (``wiki/maps/<domain>.md``): the ``-moc`` suffix is gone because the
      kind marker moved into the directory (D5). An explicit ``slug`` still wins.
    * ``daily`` → the bare ``YYYY-MM-DD``: D2.6 collapses v1's one-daily-per-domain-per-run into ONE
      journal per ``run_date``, so several v1 dailies can share this basename — that is the merge,
      not a collision. ``date`` overrides the date read off the spec (the builder passes the group
      key it merged under).

    Exported so a test can state the rename as a mapping over ``corpus.py`` rather than re-deriving
    it from two directory walks and hoping the difference is the one it meant.
    """
    if spec.kind == "moc":
        return spec.slug or spec.domain
    if spec.kind == "daily":
        return date or _daily_date(spec)
    return spec.basename()


def _daily_date(spec: NoteSpec) -> str:
    """The ``YYYY-MM-DD`` a ``daily`` spec is dated, from ``extra_frontmatter`` or its slug tail."""
    raw = spec.extra_frontmatter.get("date")
    if isinstance(raw, str) and _DAILY_DATE_RE.match(raw):
        return raw
    tail = (spec.slug or "")[-10:]
    if _DAILY_DATE_RE.match(tail):
        return tail
    raise ValueError(
        f"daily {spec.title!r}: needs slug '<domain>-YYYY-MM-DD' or a 'date' in extra_frontmatter"
    )


# --- LAYOUT (the axis the Stratum flip turns) ----------------------------------------------------


class _V1Layout:
    """The v1 wiki layout (ADR-0010 §1) — the ONLY thing here that knows about directories.

    ``wiki/<domain>/themes/<slug>.md`` · ``wiki/<domain>/daily/<domain>-YYYY-MM-DD.md`` ·
    ``wiki/<domain>/<domain>-moc.md`` · root ``index.md``. Note that ``core.wiki._is_moc_path``
    hard-codes the third of these, which is precisely why the Stratum flip changes ranking.
    """

    schema_version = 1

    def index_path(self) -> str:
        return "index.md"

    def moc_path(self, domain: str) -> str:
        return f"wiki/{domain}/{domain}-moc.md"

    def note_path(self, kind: NoteKind, domain: str, basename: str) -> str:
        if kind == "index":
            return self.index_path()
        if kind == "moc":
            return self.moc_path(domain)
        subdir = "themes" if kind == "theme" else "daily"
        return f"wiki/{domain}/{subdir}/{basename}.md"

    @staticmethod
    def link_target(from_path: str, to_path: str) -> str:
        """The relative link a bullet in ``from_path`` uses to point at ``to_path``.

        POSIX-relative from the LINKING note's directory — the shape APPLY emits (``themes/x.md``
        from a MOC, ``wiki/<d>/<d>-moc.md`` from the root index). Only the filename is load-bearing
        for resolution (basenames are the identity, ADR-0010 D5), but the realistic relative form
        keeps the fixture faithful to a curator-written repo.
        """
        base = posixpath.dirname(from_path)
        return posixpath.relpath(to_path, base or ".")


class _V2Layout:
    """The schema-2 kind-first layout (ADR-0041 D1) — the flipped axis.

    ``wiki/concepts/<slug>.md`` · ``wiki/summaries/<slug>.md`` ·
    ``wiki/notes/<yyyy>/<mm>/<date>.md`` · ``wiki/maps/<domain>.md`` · ``wiki/entities/<slug>.md`` ·
    ``wiki/people/<person>/<slug>.md`` · root ``index.md``. The DIRECTORY is the kind (D2.1) and
    the subject has left the path entirely for ``subjects:`` (D2.2/D3.2), which is why nothing here
    takes a domain except the map — where ``<domain>`` is a basename the fixture chose, not a path
    segment the schema requires.

    The kind directories come from :data:`agora_kb.core.layout.KIND_DIRECTORIES` rather than being
    re-typed here, so a fixture path can never disagree with the production path composer.
    """

    schema_version = 2

    def index_path(self) -> str:
        return "index.md"

    def map_path(self, basename: str) -> str:
        return f"wiki/{KIND_DIRECTORIES['map']}/{basename}.md"

    def note_path(self, kind: str, basename: str, *, date: str | None = None) -> str:
        """Compose the path of one schema-2 note from its KIND (never from its subject)."""
        if kind == "index":
            return self.index_path()
        if kind == "map":
            return self.map_path(basename)
        if kind == "note":
            if date is None or not _DAILY_DATE_RE.match(date):
                raise ValueError(f"kind 'note' is date-sharded; got date={date!r}")
            return f"wiki/{KIND_DIRECTORIES['note']}/{date[:4]}/{date[5:7]}/{basename}.md"
        directory = KIND_DIRECTORIES.get(kind)
        if directory is None:
            raise ValueError(f"unknown schema-2 kind {kind!r}")
        return f"wiki/{directory}/{basename}.md"

    @staticmethod
    def person_path(person: str, basename: str) -> str:
        """``wiki/people/<person>/<slug>.md`` — the human-owned namespace (ADR-0041 D3.3).

        Deliberately NOT reachable through :meth:`note_path`: production has no composer into this
        tree at all (``RepoLayout.note_path_for`` raises for ``kind='person'``), because the curator
        may never write it. The fixture needs one only to be able to PUT a human note there and
        assert that lint leaves it alone.
        """
        return f"wiki/people/{person}/{basename}.md"

    #: The relative-link rule is layout-invariant (POSIX-relative from the linking note's dir).
    link_target = staticmethod(_V1Layout.link_target)


def _layout_for(schema_version: int) -> _V1Layout | _V2Layout:
    """Return the layout object for ``schema_version`` (1 = ADR-0010, 2 = ADR-0041)."""
    if schema_version == 1:
        return _V1Layout()
    if schema_version == 2:
        return _V2Layout()
    raise ValueError(f"unknown schema_version {schema_version!r} (expected 1 or 2)")


# --- rendering (content → bytes; layout-agnostic apart from the link targets it is handed) -------


def _wikilink(name: str) -> str:
    """Normalize ``basename`` / ``[[basename]]`` to the canonical ``[[basename]]`` token."""
    stripped = name.strip()
    if stripped.startswith("[[") and stripped.endswith("]]"):
        return f"[[{stripped[2:-2].strip()}]]"
    return f"[[{stripped}]]"


def _bullet(title: str, target: str, trailing: str) -> str:
    """One MOC/index child bullet in the frozen grammar (ADR-0010 §3.2 / ADR-0014 D3)."""
    line = f"- [{title}]({target})"
    return f"{line} — {trailing}" if trailing else line


def _body_with_h1(title: str, body: str) -> str:
    """Prepend the ``# <title>`` H1 unless the body already opens with one.

    ``core.wiki`` takes a note's TITLE from the first H1 (frontmatter ``title:`` is only the
    fallback), so the H1 is what makes the title a scoring field in the fixture the way it is in a
    real Obsidian-shaped wiki.
    """
    text = body.strip("\n")
    if text.startswith("# "):
        return text
    return f"# {title}\n\n{text}" if text else f"# {title}"


def _frontmatter(spec: NoteSpec, *, children: list[str] | None = None) -> dict[str, object]:
    """Build the ADR-0010 §2 frontmatter mapping for one note, in the documented key order."""
    fm: dict[str, object] = {
        "title": spec.title,
        "type": spec.kind,
        "aliases": list(spec.aliases),
        "tags": list(spec.tags),
        "created": BUILDER_DATE,
        "updated": BUILDER_DATE,
        "status": spec.status,
        "summary": spec.summary or spec.title,
    }
    if spec.kind == "theme":
        fm["sources"] = list(spec.sources)
        fm["related"] = [_wikilink(r) for r in spec.related]
        fm["confidence"] = "high"
    elif spec.kind == "daily":
        date = spec.extra_frontmatter.get("date") or spec.basename()[-10:]
        fm["date"] = date
        fm["run_id"] = spec.extra_frontmatter.get("run_id") or f"{date}T04-00-00.000Z--f1x7ur"
    elif spec.kind in ("moc", "index"):
        fm["children"] = [_wikilink(c) for c in (children or [])]
    for key, value in spec.extra_frontmatter.items():
        if key in ("date", "run_id") and spec.kind == "daily":
            continue  # already folded in above, in the documented key position
        fm[key] = value
    return fm


def _frontmatter_v2(
    spec: NoteSpec,
    *,
    kind: str,
    kb_id: str,
    subjects: list[str],
    children: list[str] | None = None,
    date: str | None = None,
) -> dict[str, object]:
    """Build the ADR-0041 D2 frontmatter mapping for one note, in the ADR's documented key order.

    The common base is D2 verbatim — ``title``, ``kind``, ``kb``, ``subjects``, ``aliases``,
    ``tags``, ``created``, ``updated``, ``status``, ``summary``, ``derived``, ``provenance`` — and
    the per-kind additions carry over from v1 unchanged in shape.

    Three choices worth naming rather than leaving to be inferred:

    * ``type:`` is emitted as a DERIVED MIRROR of ``kind`` (**OD-3**, the recommended resolution),
      the way ADR-0014 D2 already mirrors ``summary`` into ``description``. Schema-2 lint neither
      requires nor reads it (``type:`` is RETIRED as an authority by D2.5); it exists so an OKF
      consumer reading one file in isolation still sees a type.
    * ``provenance`` is written with two EMPTY lists. That is the honest value for a synthetic
      corpus with no authenticated principal and no agent, and it keeps D2.3's split visible on
      disk; a spec that wants real values passes them in ``extra_frontmatter``.
    * ``subjects`` is supplied by the CALLER, never derived here, because deriving it from a path is
      exactly what D3.2 abolishes.
    """
    fm: dict[str, object] = {
        "title": spec.title,
        "kind": kind,
        "type": kind,
        "kb": kb_id,
        "subjects": list(subjects),
        "aliases": list(spec.aliases),
        "tags": list(spec.tags),
        "created": BUILDER_DATE,
        "updated": BUILDER_DATE,
        "status": spec.status,
        "summary": spec.summary or spec.title,
        "derived": False,
        "provenance": {"writers": [], "agents": []},
    }
    if kind in ("concept", "summary"):
        fm["sources"] = list(spec.sources)
        fm["related"] = [_wikilink(r) for r in spec.related]
        fm["confidence"] = "high"
    elif kind == "entity":
        # `sources:` MAY be empty on an entity (D2) — L1-7's non-empty rule excludes the kind.
        fm["sources"] = list(spec.sources)
        fm["related"] = [_wikilink(r) for r in spec.related]
    elif kind == "note":
        fm["date"] = date
        run_id = spec.extra_frontmatter.get("run_id")
        fm["run_id"] = run_id if isinstance(run_id, str) else f"{date}T04-00-00.000Z--f1x7ur"
        fm["sources"] = list(spec.sources)
    elif kind in ("map", "index"):
        fm["children"] = [_wikilink(c) for c in (children or [])]
    for key, value in spec.extra_frontmatter.items():
        if kind == "note" and key in ("date", "run_id"):
            continue  # already folded in above, in the documented key position
        fm[key] = value
    return fm


def _write(root: Path, rel: str, text: str) -> None:
    """Write one file as UTF-8 / LF / no BOM (L1-16), creating parents."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _materialize_sources(root: Path, basename: str, sources: list[str]) -> None:
    """Create any ``raw/`` evidence file a note cites that does not exist yet (L1-7 / L1-8)."""
    for src in sources:
        if not (root / src).exists():
            _write(root, src, f"# evidence: {basename}\n\nSynthetic evidence artifact.\n")


# --- the builder ---------------------------------------------------------------------------------


def build_kb(
    root: Path,
    notes: list[NoteSpec],
    *,
    schema_version: int = 1,
    domains: list[str] | None = None,
    kb_id: str = FIXTURE_KB_ID,
    kb_name: str = FIXTURE_KB_NAME,
) -> Path:
    """Materialize a complete, lint-clean knowledge repo at ``root`` and return ``root``.

    ``notes`` carries the CONTENT (theme/daily specs, plus optional ``moc``/``index`` overrides and,
    on schema 2, ``summary``/``entity``/``person`` specs); ``schema_version`` selects the LAYOUT
    (1 = the ADR-0010 v1 wiki layout, 2 = the ADR-0041 kind-first layout). ``domains`` fixes the
    domain order/set written into ``_meta/taxonomy.yaml`` (default: every domain named by a spec,
    sorted). ``kb_id``/``kb_name`` are the schema-2 ``_meta/kb.yaml`` identity (D1.5) and are
    REJECTED on a schema-1 build, which has no such file — a silently-ignored argument is how a
    fixture ends up asserting something it never wrote.

    A map links exactly the notes named by its ``moc`` spec's ``children`` (or, absent such a spec,
    EVERY concept — and, on schema 2, summary — of that domain, in spec order), so a note
    deliberately left out is an orphan with no inbound link: a shape the ranker must be pinned
    against.

    The DEFAULT is deliberately ``schema_version=1``, not 2: this builder's first job is still to
    materialize the pre-flip corpus the gate-B golden was recorded over, and flipping the default
    would silently re-record it.

    Raises :class:`ValueError` on a corpus that could not lint (duplicate basename, a map child that
    is not an admitted child of that domain, an unusable daily basename, a schema-2-only kind under
    schema 1) — the fixture fails loudly at build time rather than as a mystery lint finding later.
    """
    if schema_version == 2:
        return _build_v2(Path(root), notes, domains=domains, kb_id=kb_id, kb_name=kb_name)
    if kb_id != FIXTURE_KB_ID or kb_name != FIXTURE_KB_NAME:
        raise ValueError(
            "kb_id/kb_name are schema-2 only: _meta/kb.yaml does not exist in the v1 layout "
            "(ADR-0041 D1.5)"
        )
    return _build_v1(Path(root), notes, domains=domains, schema_version=schema_version)


def _build_v1(
    root: Path,
    notes: list[NoteSpec],
    *,
    schema_version: int,
    domains: list[str] | None,
) -> Path:
    """Materialize the ADR-0010 v1 layout. Byte-identical to the pre-Stratum builder."""
    layout_rules = _layout_for(schema_version)

    theme_specs: dict[str, list[NoteSpec]] = {}
    daily_specs: dict[str, list[NoteSpec]] = {}
    moc_specs: dict[str, NoteSpec] = {}
    index_spec: NoteSpec | None = None
    for spec in notes:
        if spec.kind in ("summary", "entity", "person"):
            raise ValueError(
                f"kind {spec.kind!r} ({spec.title!r}) exists only in KB wiki schema 2 (ADR-0041 "
                f"D2.5); the v1 layout has no directory for it"
            )
        if spec.kind == "theme":
            theme_specs.setdefault(spec.domain, []).append(spec)
        elif spec.kind == "daily":
            daily_specs.setdefault(spec.domain, []).append(spec)
        elif spec.kind == "moc":
            if spec.domain in moc_specs:
                raise ValueError(f"two moc specs for domain {spec.domain!r}")
            moc_specs[spec.domain] = spec
        else:
            if index_spec is not None:
                raise ValueError("two index specs")
            index_spec = spec

    spec_domains = {s.domain for s in notes if s.kind != "index"}
    if domains is None:
        all_domains = sorted(spec_domains)
    else:
        unknown = sorted(spec_domains - set(domains))
        if unknown:
            raise ValueError(f"specs use domains outside `domains`: {unknown}")
        all_domains = list(domains)

    # basename uniqueness (L1-1) + the alias/basename union (L1-15) checked up front.
    basenames: dict[str, str] = {}
    for spec in notes:
        if spec.kind in ("moc", "index"):
            continue
        base = spec.basename()
        if base in basenames:
            raise ValueError(
                f"duplicate basename {base!r} ({spec.title!r} and {basenames[base]!r})"
            )
        basenames[base] = spec.title
    for domain in all_domains:
        basenames.setdefault(f"{domain}-moc", f"{domain} MOC")
    basenames.setdefault("index", "index")
    seen_names = set(basenames)
    for spec in notes:
        for alias in spec.aliases:
            if alias in seen_names:
                raise ValueError(f"alias {alias!r} collides with an existing basename/alias")
            seen_names.add(alias)

    # --- schema / taxonomy: the real admin path, so the header and the taxonomy agree (L1-17) ---
    repo_layout = RepoLayout(root)
    root.mkdir(parents=True, exist_ok=True)
    taxonomy = Taxonomy(
        schema_version=layout_rules.schema_version,
        taxonomy_policy="open",
        allowed_tags=tuple(sorted({t for s in notes for t in s.tags})),
        domains=tuple(all_domains),
    )
    emit_schema(repo_layout, taxonomy=taxonomy, force=True)

    # --- themes + dailies -----------------------------------------------------------------------
    for domain in all_domains:
        for spec in theme_specs.get(domain, []) + daily_specs.get(domain, []):
            base = spec.basename()
            rel = layout_rules.note_path(spec.kind, domain, base)
            written = spec
            if spec.kind == "theme":
                sources = list(spec.sources)
                if not sources and spec.status != "stub":
                    sources = [f"raw/{domain}/{base}.md"]
                written = NoteSpec(**{**spec.__dict__, "sources": sources})
                for src in sources:
                    if not (root / src).exists():
                        _write(root, src, f"# evidence: {base}\n\nSynthetic evidence artifact.\n")
            _write(
                root, rel, render(_frontmatter(written), _body_with_h1(written.title, written.body))
            )

    # --- per-domain MOCs (children == child bullets, THEMES only) -------------------------------
    for domain in all_domains:
        themes = theme_specs.get(domain, [])
        by_base = {s.basename(): s for s in themes}
        override = moc_specs.get(domain)
        if override is not None and override.children:
            selected = []
            for child in override.children:
                key = child.strip().strip("[]").strip()
                if key not in by_base:
                    raise ValueError(
                        f"moc {domain!r} lists child {key!r}, not a theme of {domain!r}"
                    )
                selected.append(by_base[key])
        else:
            selected = list(themes)

        moc_rel = layout_rules.moc_path(domain)
        bullets = []
        for s in selected:
            target = layout_rules.note_path("theme", domain, s.basename())
            label = s.moc_label or s.title
            bullets.append(_bullet(label, layout_rules.link_target(moc_rel, target), s.summary))
        spec = override or NoteSpec(
            kind="moc",
            domain=domain,
            title=f"{domain} MOC",
            body="",
            summary=f"Map of content for the {domain} domain.",
        )
        prose = spec.body.strip("\n")
        body = "\n".join(bullets)
        if prose:
            body = f"{prose}\n\n{body}"
        fm = _frontmatter(
            NoteSpec(**{**spec.__dict__, "kind": "moc", "domain": domain, "slug": f"{domain}-moc"}),
            children=[s.basename() for s in selected],
        )
        _write(root, moc_rel, render(fm, _body_with_h1(spec.title, body)))

    # --- root index.md (children == the domain MOCs) --------------------------------------------
    index_rel = layout_rules.index_path()
    spec = index_spec or NoteSpec(
        kind="index",
        domain="",
        title="Knowledge base",
        body="",
        summary="Root map of every domain in this knowledge base.",
    )
    bullets = [
        _bullet(
            f"{domain} MOC",
            layout_rules.link_target(index_rel, layout_rules.moc_path(domain)),
            f"Map of content for the {domain} domain.",
        )
        for domain in all_domains
    ]
    prose = spec.body.strip("\n")
    body = "\n".join(bullets)
    if prose:
        body = f"{prose}\n\n{body}"
    fm = _frontmatter(
        NoteSpec(**{**spec.__dict__, "kind": "index", "slug": "index"}),
        children=[f"{domain}-moc" for domain in all_domains],
    )
    _write(root, index_rel, render(fm, _body_with_h1(spec.title, body)))

    return root


# --- the schema-2 builder (ADR-0041) -------------------------------------------------------------


def _merge_journal(date: str, contributors: list[NoteSpec], subjects: list[str]) -> NoteSpec:
    """Collapse every same-dated v1 daily into ONE schema-2 journal spec (ADR-0041 D2.6 / D6 §4).

    v1 wrote one daily per domain per run and namespaced the basename ``<domain>-YYYY-MM-DD``
    *because* bare dates would have collided across domains. Schema 2 removes the domain from the
    path, so the collision reason is gone and the journal becomes one note per ``run_date``,
    repo-wide — which is what makes the note↔``run_id`` relation 1:1 and lets L1-14's successor be a
    clean identity check.

    The merge rule, stated so the degenerate case is visibly an identity: **lists union in order**
    (``tags``, ``aliases``, ``sources``, and ``subjects`` from the contributing domains), **the body
    concatenates one ``## <contributor title>`` section per contributor in domain order**, and every
    remaining scalar is taken from the FIRST contributor in domain order. With a single contributor
    — the corpus's own case, since its three dailies carry three different dates — every clause
    above reduces to "that daily's own value", and the only thing that changed is the basename.

    Each contributor's TITLE becomes its section heading rather than being dropped: a merge that
    discards the titles of the notes it merges is a lossy migration wearing a merge's name.
    """
    first = contributors[0]
    sections = [f"## {c.title}\n\n{c.body.strip(chr(10))}".rstrip() for c in contributors]
    extra: dict[str, object] = {}
    for c in contributors:
        extra.update(c.extra_frontmatter)
    extra["date"] = date
    return NoteSpec(
        kind="daily",
        domain=first.domain,
        title=date,
        body="\n\n".join(sections),
        slug=date,
        aliases=_ordered_union(c.aliases for c in contributors),
        tags=_ordered_union(c.tags for c in contributors),
        summary=" ".join(dict.fromkeys(c.summary for c in contributors if c.summary)),
        sources=_ordered_union(c.sources for c in contributors),
        status=first.status,
        extra_frontmatter=extra,
    )


def _ordered_union(lists: Iterable[list[str]]) -> list[str]:
    """Concatenate ``lists`` preserving first-seen order and dropping repeats (a stable union)."""
    out: dict[str, None] = {}
    for items in lists:
        for item in items:
            out.setdefault(item, None)
    return list(out)


def _build_v2(
    root: Path,
    notes: list[NoteSpec],
    *,
    domains: list[str] | None,
    kb_id: str,
    kb_name: str,
) -> Path:
    """Materialize the ADR-0041 kind-first layout from the SAME specs the v1 builder consumes.

    The correspondence is total and mechanical: a ``theme`` becomes a ``concept`` under
    ``wiki/concepts/``, its domain becomes ``subjects: [<domain>]``, a ``moc`` becomes a ``map`` at
    ``wiki/maps/<domain>.md``, and same-dated ``daily`` specs merge into one journal on its
    ``<yyyy>/<mm>`` shard. ``raw/`` is written exactly as v1 writes it, because D1.4 never moves it.
    """
    layout_rules = _V2Layout()

    concept_specs: dict[str, list[NoteSpec]] = {}
    summary_specs: dict[str, list[NoteSpec]] = {}
    entity_specs: list[NoteSpec] = []
    person_specs: list[NoteSpec] = []
    daily_specs: dict[str, list[NoteSpec]] = {}
    map_specs: dict[str, NoteSpec] = {}
    index_spec: NoteSpec | None = None
    for spec in notes:
        if spec.kind == "theme":
            concept_specs.setdefault(spec.domain, []).append(spec)
        elif spec.kind == "summary":
            summary_specs.setdefault(spec.domain, []).append(spec)
        elif spec.kind == "entity":
            entity_specs.append(spec)
        elif spec.kind == "person":
            if not (spec.person or spec.domain):
                raise ValueError(
                    f"person {spec.title!r}: needs `person=` — it is the wiki/people/<person>/ "
                    f"namespace segment (ADR-0041 D3.3)"
                )
            person_specs.append(spec)
        elif spec.kind == "daily":
            daily_specs.setdefault(spec.domain, []).append(spec)
        elif spec.kind == "moc":
            if spec.domain in map_specs:
                raise ValueError(f"two moc specs for domain {spec.domain!r}")
            map_specs[spec.domain] = spec
        else:
            if index_spec is not None:
                raise ValueError("two index specs")
            index_spec = spec

    # `person` is excluded from the domain vocabulary: a people note is outside the curated wiki and
    # carries no subject the taxonomy has to declare (D3.3).
    spec_domains = {s.domain for s in notes if s.kind not in ("index", "person") and s.domain}
    if domains is None:
        all_domains = sorted(spec_domains)
    else:
        unknown = sorted(spec_domains - set(domains))
        if unknown:
            raise ValueError(f"specs use domains outside `domains`: {unknown}")
        all_domains = list(domains)
    reserved = sorted(d for d in all_domains if d.startswith("_"))
    if reserved:
        raise ValueError(
            f"domains {reserved} begin with '_', which is RESERVED for the raw/ prefix namespace "
            f"(raw/_blob, raw/_pages) — lint L1-23 / ADR-0041 D1.4"
        )

    # The journals, merged BEFORE the uniqueness check so a merged basename is checked once (D2.6).
    journals: dict[str, list[NoteSpec]] = {}
    journal_subjects: dict[str, list[str]] = {}
    for domain in all_domains:
        for spec in daily_specs.get(domain, []):
            date = _daily_date(spec)
            journals.setdefault(date, []).append(spec)
            subjects = journal_subjects.setdefault(date, [])
            if domain not in subjects:
                subjects.append(domain)
    merged_journals = {
        date: _merge_journal(date, contributors, journal_subjects[date])
        for date, contributors in journals.items()
    }

    # --- basename uniqueness (L1-1) + the alias/basename union (L1-15), checked up front ---------
    #
    # `person` basenames and aliases are deliberately ABSENT from both checks: ADR-0041 D3.3 puts
    # `wiki/people/**` outside the global `[[basename]]` identity space, so a human file may share a
    # basename with a curated note. Reproducing that here is what keeps the fixture able to build
    # the shape lint is specified to tolerate.
    basenames: dict[str, str] = {}

    def _claim(base: str, title: str) -> None:
        if base in basenames:
            raise ValueError(f"duplicate basename {base!r} ({title!r} and {basenames[base]!r})")
        basenames[base] = title

    for spec in notes:
        if spec.kind in ("moc", "index", "daily", "person"):
            continue
        _claim(spec.basename(), spec.title)
    for date, journal in merged_journals.items():
        _claim(date, journal.title)
    map_basenames = {d: (map_specs[d].slug or d) if d in map_specs else d for d in all_domains}
    for domain in all_domains:
        _claim(map_basenames[domain], f"{domain} map")
    _claim("index", "index")
    seen_names = set(basenames)
    for spec in notes:
        if spec.kind == "person":
            continue
        for alias in spec.aliases:
            if alias in seen_names:
                raise ValueError(f"alias {alias!r} collides with an existing basename/alias")
            seen_names.add(alias)

    # --- schema / taxonomy / KB identity: the real admin path (L1-17 + ADR-0041 D1.5) ------------
    repo_layout = RepoLayout(root)
    root.mkdir(parents=True, exist_ok=True)
    taxonomy = Taxonomy(
        schema_version=layout_rules.schema_version,
        taxonomy_policy="open",
        allowed_tags=tuple(sorted({t for s in notes for t in s.tags})),
        domains=tuple(all_domains),
    )
    emit_schema(repo_layout, taxonomy=taxonomy, force=True)
    write_kb_identity(repo_layout, KbIdentity(kb_id=kb_id, name=kb_name, declared_kind="personal"))

    # The kind directories exist even when empty: `wiki/summaries/` and `wiki/entities/` ship EMPTY
    # (OD-7 / OD-8) and `wiki/people/` is populated only by a human, so the CONTAINER is the schema
    # and the population is not.
    for directory in (*KIND_DIRECTORIES.values(), "people"):
        (root / "wiki" / directory).mkdir(parents=True, exist_ok=True)

    # --- concepts + summaries + entities (flat under their kind directory, D1.1) -----------------
    for kind, buckets in (("concept", concept_specs), ("summary", summary_specs)):
        for domain in all_domains:
            for spec in buckets.get(domain, []):
                base = spec.basename()
                sources = list(spec.sources)
                if not sources and spec.status != "stub":
                    sources = [f"raw/{domain}/{base}.md"]
                _materialize_sources(root, base, sources)
                written = NoteSpec(**{**spec.__dict__, "sources": sources})
                _write(
                    root,
                    layout_rules.note_path(kind, base),
                    render(
                        _frontmatter_v2(written, kind=kind, kb_id=kb_id, subjects=[domain]),
                        _body_with_h1(written.title, written.body),
                    ),
                )
    for spec in entity_specs:
        base = spec.basename()
        _materialize_sources(root, base, list(spec.sources))
        subjects = [spec.domain] if spec.domain else []
        _write(
            root,
            layout_rules.note_path("entity", base),
            render(
                _frontmatter_v2(spec, kind="entity", kb_id=kb_id, subjects=subjects),
                _body_with_h1(spec.title, spec.body),
            ),
        )

    # --- the human-owned tree: written, never graded (D3.3) --------------------------------------
    for spec in person_specs:
        base = spec.basename()
        person = spec.person or spec.domain
        subjects = [spec.domain] if spec.domain in all_domains else []
        _write(
            root,
            layout_rules.person_path(person, base),
            render(
                _frontmatter_v2(spec, kind="person", kb_id=kb_id, subjects=subjects),
                _body_with_h1(spec.title, spec.body),
            ),
        )

    # --- the journals, one per run_date, on their <yyyy>/<mm> shard (D1.1 / D2.6) -----------------
    for date in sorted(merged_journals):
        journal = merged_journals[date]
        _materialize_sources(root, date, list(journal.sources))
        _write(
            root,
            layout_rules.note_path("note", date, date=date),
            render(
                _frontmatter_v2(
                    journal,
                    kind="note",
                    kb_id=kb_id,
                    subjects=journal_subjects[date],
                    date=date,
                ),
                _body_with_h1(journal.title, journal.body),
            ),
        )

    # --- maps (children == child bullets, and every child kind ADMITTED by D1.3 / L1-24) ----------
    for domain in all_domains:
        candidates = [s for s in notes if s.domain == domain and s.kind in ("theme", "summary")]
        by_base = {s.basename(): s for s in candidates}
        override = map_specs.get(domain)
        if override is not None and override.children:
            selected = []
            for child in override.children:
                key = child.strip().strip("[]").strip()
                if key not in by_base:
                    raise ValueError(
                        f"map {domain!r} lists child {key!r}, not an admitted child of "
                        f"{domain!r} (ADR-0041 D1.3 admits concept/summary/map only)"
                    )
                selected.append(by_base[key])
        else:
            selected = list(candidates)

        map_rel = layout_rules.map_path(map_basenames[domain])
        bullets = []
        for s in selected:
            target = layout_rules.note_path(V1_KIND_TO_V2_KIND[s.kind], s.basename())
            label = s.moc_label or s.title
            bullets.append(_bullet(label, layout_rules.link_target(map_rel, target), s.summary))
        spec = override or NoteSpec(
            kind="moc",
            domain=domain,
            title=f"{domain} MOC",
            body="",
            summary=f"Map of content for the {domain} domain.",
        )
        prose = spec.body.strip("\n")
        body = "\n".join(bullets)
        if prose:
            body = f"{prose}\n\n{body}"
        fm = _frontmatter_v2(
            NoteSpec(**{**spec.__dict__, "kind": "moc", "domain": domain}),
            kind="map",
            kb_id=kb_id,
            subjects=[domain],
            children=[s.basename() for s in selected],
        )
        _write(root, map_rel, render(fm, _body_with_h1(spec.title, body)))

    # --- root index.md: the ROOT MAP, outside wiki/ and outside maps/ (D1.2) ----------------------
    index_rel = layout_rules.index_path()
    spec = index_spec or NoteSpec(
        kind="index",
        domain="",
        title="Knowledge base",
        body="",
        summary="Root map of every domain in this knowledge base.",
    )
    bullets = [
        _bullet(
            f"{domain} MOC",
            layout_rules.link_target(index_rel, layout_rules.map_path(map_basenames[domain])),
            f"Map of content for the {domain} domain.",
        )
        for domain in all_domains
    ]
    prose = spec.body.strip("\n")
    body = "\n".join(bullets)
    if prose:
        body = f"{prose}\n\n{body}"
    fm = _frontmatter_v2(
        NoteSpec(**{**spec.__dict__, "kind": "index", "slug": "index"}),
        kind="index",
        kb_id=kb_id,
        # D2.2: the root map is filed under no subject. `[]` asserts nothing and loses nothing.
        subjects=[],
        children=[map_basenames[domain] for domain in all_domains],
    )
    _write(root, index_rel, render(fm, _body_with_h1(spec.title, body)))

    return root
