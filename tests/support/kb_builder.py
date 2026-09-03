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
* **LAYOUT** is a private layout object selected by ``schema_version`` — it decides paths and the
  relative link targets that MOC/index bullets carry. ``schema_version=2`` raises
  :class:`NotImplementedError` until UNIT 2 lands.

What UNIT 2 will and will not have to touch, stated precisely so the split is not oversold.
NOT touched: :mod:`tests.rank_golden.corpus` — no title, body, tag, alias or MOC label moves, which
is the property that makes a ranking delta attributable to the layout (pinned by
``test_stratum_layout_is_not_implemented_yet``). TOUCHED, beyond adding a layout class: the v1
BASENAME grammar (:meth:`NoteSpec.basename` returns ``<domain>-moc`` / ``<domain>-YYYY-MM-DD``) and
the NAVIGATION model in :func:`build_kb` — "one MOC per domain, and the root index lists the domain
MOCs" is hard-coded in the per-domain MOC loop and the index block. The Stratum target moves the
KIND into the path segment and the subject into frontmatter, so that navigation model is itself
part of what changes; UNIT 2 should expect to lift those into the layout object (a
``moc_basename(domain)`` / ``navigation_groups(specs)`` seam) rather than to add one class and stop.

What "complete" means here is exactly what the real L1 linter
(:func:`agora_kb.schema.lint.lint`) demands of a v1 repo, so the fixture can never drift into a
shape the curator would reject:

* the real schema emitter (:func:`agora_kb.schema.emit.emit_schema`) writes ``AGENTS.md`` (+ its
  agent-guide symlinks), ``_meta/taxonomy.yaml`` and ``_templates/`` — the same admin/init path
  ``agora repo init`` runs, so the schema-doc header and the taxonomy agree (L1-17);
* the taxonomy's ``allowed_tags`` is the union of every tag the specs use and its ``domains`` the
  set of domains, so tags/domains are in-vocabulary by construction (L1-5);
* every non-stub theme gets non-empty ``sources:`` and the ``raw/<domain>/<event>.md`` evidence
  files those sources name are materialized (L1-7 / L1-8);
* each domain MOC's ``children:`` is generated from the SAME list as its body child bullets, so the
  two sides cannot disagree (L1-6); likewise the root ``index.md`` over the domain MOCs.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# The PRODUCTION slugger and the PRODUCTION canonical hash, imported rather than re-implemented.
# A private-looking `_slugify` is reached into on purpose: a second slugger in the fixture would
# drift from the one the curator/import paths actually use (#57 — the real one caps at 60 chars and
# rejects a slug that fails `_SLUG_OK_RE`), and a fixture that claims to carry the "#57 shape" while
# computing a different basename pins nothing about it.
from agora_kb.adapters.ollama_brain import _slugify as _production_slugify
from agora_kb.core.frontmatter import render
from agora_kb.core.hashing import content_sha256
from agora_kb.core.layout import RepoLayout
from agora_kb.schema.emit import Taxonomy, emit_schema

__all__ = [
    "BUILDER_DATE",
    "NoteKind",
    "NoteSpec",
    "build_kb",
    "hash_basename",
    "slugify",
]

NoteKind = Literal["theme", "daily", "moc", "index"]

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
    extra_frontmatter: dict[str, object] = field(default_factory=dict)

    def basename(self) -> str:
        """The note's globally-unique basename (ADR-0010 D5), independent of layout."""
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


# --- LAYOUT (the axis UNIT 2 flips) --------------------------------------------------------------


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


def _layout_for(schema_version: int) -> _V1Layout:
    """Return the layout object for ``schema_version`` (the UNIT 2 seam)."""
    if schema_version == 1:
        return _V1Layout()
    if schema_version == 2:
        raise NotImplementedError("Stratum layout lands in UNIT 2")
    raise ValueError(f"unknown schema_version {schema_version!r} (expected 1, or 2 in UNIT 2)")


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


def _write(root: Path, rel: str, text: str) -> None:
    """Write one file as UTF-8 / LF / no BOM (L1-16), creating parents."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


# --- the builder ---------------------------------------------------------------------------------


def build_kb(
    root: Path,
    notes: list[NoteSpec],
    *,
    schema_version: int = 1,
    domains: list[str] | None = None,
) -> Path:
    """Materialize a complete, lint-clean knowledge repo at ``root`` and return ``root``.

    ``notes`` carries the CONTENT (theme/daily specs, plus optional ``moc``/``index`` overrides);
    ``schema_version`` selects the LAYOUT (1 = the v1 wiki layout; 2 raises until UNIT 2 lands).
    ``domains`` fixes the domain order/set written into ``_meta/taxonomy.yaml`` (default: every
    domain named by a spec, sorted).

    A domain's MOC links exactly the themes named by its ``moc`` spec's ``children`` (or, absent
    such a spec, EVERY theme of that domain, in spec order) — so a theme deliberately left out of
    its MOC is an orphan with no inbound link, which is a shape the ranker must be pinned against.

    Raises :class:`ValueError` on a corpus that could not lint (duplicate basename, a MOC child
    that is not a theme of that domain, an unusable daily basename) — the fixture fails loudly at
    build time rather than as a mystery lint finding later.
    """
    layout_rules = _layout_for(schema_version)
    root = Path(root)

    theme_specs: dict[str, list[NoteSpec]] = {}
    daily_specs: dict[str, list[NoteSpec]] = {}
    moc_specs: dict[str, NoteSpec] = {}
    index_spec: NoteSpec | None = None
    for spec in notes:
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
