"""Recover the ``tags:`` an Obsidian/markdown vault holds, keyed by note BASENAME (issue #174).

The vault importer never widens a destination's taxonomy: a source tag absent from
``allowed_tags`` is stripped and recorded (:func:`~agora_kb.ingest.vault_import._filter_tags`,
ADR-0014 D5 auto-fix #6). Importing into a fresh repo therefore drops EVERY tag, because a fresh
``_meta/taxonomy.yaml`` declares ``allowed_tags: {}``. This module is the read half of the way
back: it indexes the source vault by basename and answers, per destination note, what the source
held — and the curator engine (:mod:`agora_kb.curator.restamp`) decides what to do with the answer.

**The inverse of the loss is not a normalisation.** ``_filter_tags`` is a pure membership filter: it
lower-cases nothing, kebab-cases nothing, and preserves order. So the tags surfaced here are the
source's own strings, verbatim and de-duplicated, and a tag that is not kebab-case is REPORTED
(:attr:`~agora_kb.curator.restamp.TagMatch.invalid_tags`) rather than repaired — inventing a
normalisation the importer never had would be a new editorial decision dressed as a recovery.

**Frontmatter is read with the importer's tolerant reader, and that is load-bearing.** A real
Obsidian vault routinely carries ``links: [[a]], [[b]]`` in its frontmatter, which is not valid
YAML: on the owner's own vault, seven of the nine matching notes raise
:class:`~agora_kb.core.frontmatter.FrontmatterError` under a plain
:func:`~agora_kb.core.frontmatter.parse`. :func:`~agora_kb.ingest.vault_import._repair_obsidian_
frontmatter` repairs that shape, re-parses, and degrades to empty frontmatter rather than raising —
so an implementation that reaches for ``parse`` silently recovers one note in nine.

Layering: this module knows about vaults and imports :class:`~agora_kb.curator.restamp.TagMatch`
for its return type ONLY. The engine deliberately does not import ``ingest`` (there is not one
import between the two packages today, and a maintenance command is a poor reason to open that
edge), so the dependency runs this way and is exactly one dataclass wide; the face wires a
:class:`VaultTagIndex` into ``run_restamp`` as a :class:`~agora_kb.curator.restamp.TagSource`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..curator.restamp import TagMatch
from .vault_import import (
    _EXEMPT_STEMS,
    _STRUCTURAL_DIRS,
    _repair_obsidian_frontmatter,
    _slugify,
)

__all__ = ["VaultTagIndex", "build_vault_tag_index", "iter_vault_notes"]

# A kebab-case tag: [a-z0-9] words joined by single '-'. Mirrors `core.models.InboxItem._KEBAB_RE`
# (and `faces.web.app._KEBAB_TAG_RE`, which mirrors it for the same reason): a tag that reaches
# `allowed_tags` without matching this passes lint L1-5 — which checks MEMBERSHIP only, and has no
# rule about tag SHAPE — while making the repo one that `InboxItem` and the web upload face then
# refuse. Recovery must not be able to create that repo.
_KEBAB_TAG_RE = re.compile(r"\A[a-z0-9]+(-[a-z0-9]+)*\Z")

# The `raw/` capture tier, excluded from the walk. NET-NEW relative to the importer's own rules and
# load-bearing: `_STRUCTURAL_DIRS` does NOT list `raw`, so the importer walk descends into it. A
# vault that is itself an Agora repo has `raw/<domain>/<slug>.md` and `wiki/**/<slug>.md` sharing a
# basename BY CONSTRUCTION (the concept cites the capture it was made from), so without this rule
# nearly every lookup in a populated vault would come back `ambiguous` and recover nothing.
_RAW_DIRNAME = "raw"

# The HUMAN-owned namespace of a vault that is itself a schema-2 Agora repo. ADR-0041 D3.3 puts
# `wiki/people/**` basenames OUTSIDE the global `[[basename]]` identity space, so a person note is
# not a candidate for ANY curated basename — and indexing it would give a same-named concept that
# person's tags, unioning private vocabulary into the destination's public `allowed_tags`. The
# engine already refuses to WRITE that namespace (`restamp.plan_restamp` raises on it); this is the
# same boundary on the READ side, without which the fence only holds in one direction.
_PEOPLE_PREFIX = ("wiki", "people")


def iter_vault_notes(src: Path) -> Iterator[Path]:
    """Yield every vault path that counts as a NOTE, in deterministic (sorted) order.

    The importer's own seven-step walk (``vault_import.import_vault``), so the population indexed
    here is the population that was imported, plus two exclusions this reader adds for the vault
    that is itself an Agora repo (``raw/`` above, ``wiki/people/**`` beside it):

    1. ``sorted(src.rglob("*.md"))``;
    2. skip anything with a dot-leading path component (``.obsidian/``, ``.git/``, dotfiles);
    3. skip symlinks (``CLAUDE.md -> AGENTS.md`` and friends are never notes);
    4. skip non-files;
    5. skip an exempt STEM — the schema doc, its agent-guide aliases, and ``log.md``;
    6. skip anything under ``_templates/`` / ``_meta/`` / ``_kb/`` at any depth;
    7. skip anything whose FIRST component is ``raw``;
    8. skip ``wiki/people/**`` (ADR-0041 D3.3 — outside the basename identity space).
    """
    for path in sorted(src.rglob("*.md")):
        rel_parts = path.relative_to(src).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        if path.stem in _EXEMPT_STEMS:
            continue
        if any(part in _STRUCTURAL_DIRS for part in rel_parts[:-1]):
            continue
        if rel_parts[:1] == (_RAW_DIRNAME,):
            continue
        if rel_parts[:2] == _PEOPLE_PREFIX:
            continue
        yield path


def _vault_tags(value: object) -> tuple[str, ...]:
    """The importer's tag rule applied in reverse: list-of-string only, order kept, de-duplicated.

    A scalar ``tags: research`` yields nothing, and so does a list entry that is not a string —
    because ``_filter_tags`` dropped both on the way in (it iterates a list and skips non-strings,
    and replaces a non-list value with ``[]``). An inverse that resurrected them would be adding
    tags the import never had, not recovering ones it removed.
    """
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(v for v in value if isinstance(v, str)))


def _index_keys(stem: str) -> tuple[str, ...]:
    """The keys one vault note answers to: its RAW stem and the importer's DESTINATION slug.

    Both, because the two are not the same string and the destination is what a restamp looks up.
    ``vault_import._infer_layout`` composes a catch-all note's basename with :func:`_slugify` (so
    ``Foo Bar.md`` lands at ``wiki/concepts/foo-bar.md``) while a ``wiki/<d>/themes/<slug>.md`` note
    keeps its filename stem verbatim. Indexing the raw stem ALONE therefore lets a slugified
    destination collide with some OTHER vault note's literal stem and be answered ``matched`` from
    the wrong note — the silent mis-attachment the ``ambiguous`` rule exists to prevent. With both
    keys present that collision has two candidates and comes back ``ambiguous``, which is a skip
    and a named pair of paths rather than a wrong tag list.

    Deduplicated and ordered, so the common case (a stem that is already its own slug) inserts ONE
    entry — a note that answered to itself twice would make every lookup ambiguous. An empty slug
    (a filename with nothing path-safe in it) is dropped: the importer falls back to a content hash
    there, which no basename join can reproduce.
    """
    return tuple(key for key in dict.fromkeys((stem, _slugify(stem))) if key)


@dataclass(frozen=True)
class VaultTagIndex:
    """A basename → ``(relative path, tags)`` index over one vault. Satisfies ``TagSource``.

    Built once and queried per note. Basenames are the join key because that is the only identity
    that survives the import: the destination's ``raw/`` copies carry the extracted BODY with no
    frontmatter at all, and the schema-2 flip moved the subject out of the path — so a path-based
    match has nothing to match on, while the note basename is unchanged by construction (globally
    unique on both sides, ADR-0010 §3.1). "Unchanged" means the importer's own naming rule, which
    is why each note is indexed under :func:`_index_keys` rather than under its filename alone.
    """

    root: Path
    by_basename: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = field(default_factory=dict)

    def lookup(self, basename: str) -> TagMatch:
        """Answer for one destination basename. Never raises; never guesses.

        Two or more candidates is ``ambiguous`` and names them all: picking one would silently
        attach some other note's tags, and the operator can tidy the vault and re-run — the tag
        bridge is deliberately re-entrant rather than one-shot, so a skipped note is recoverable
        later, not lost.
        """
        hits = self.by_basename.get(basename, ())
        if not hits:
            return TagMatch(status="unmatched")
        if len(hits) > 1:
            return TagMatch(status="ambiguous", source=", ".join(rel for rel, _tags in hits))
        rel, tags = hits[0]
        if not tags:
            return TagMatch(status="no-tags", source=rel)
        invalid = tuple(t for t in tags if not _KEBAB_TAG_RE.match(t))
        return TagMatch(status="matched", tags=tags, source=rel, invalid_tags=invalid)


def build_vault_tag_index(src: Path) -> VaultTagIndex:
    """Walk ``src`` once and build the basename index. READ-ONLY: the vault is never written.

    Every note is read with ``errors="replace"`` before the tolerant frontmatter repair, mirroring
    the importer: a vault saved as latin-1/cp1252 must degrade to a mangled character, never to a
    :class:`UnicodeDecodeError` out of a recovery pass.
    """
    src = Path(src)
    index: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for path in iter_vault_notes(src):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm, _body, _repaired = _repair_obsidian_frontmatter(text)
        rel = path.relative_to(src).as_posix()
        hit = (rel, _vault_tags(fm.get("tags")))
        for key in _index_keys(path.stem):
            index.setdefault(key, []).append(hit)
    return VaultTagIndex(root=src, by_basename={name: tuple(hits) for name, hits in index.items()})
