"""Schema emitter — the repo-init / admin path that writes the editorial schema into a KB repo.

``emit_schema`` materializes Layer 3 of the Karpathy LLM-wiki (ADR-0010 §0): the editorial schema
doc (``AGENTS.md`` + the ``CLAUDE.md`` / ``QWEN.md`` / ``GEMINI.md`` symlinks), the fixed read-only
``_meta/taxonomy.yaml`` (the closed tag/domain vocabulary, ADR-0010 §5), and the ``_templates/``
note templates. It writes FROM a packaged template so the emitted doc is a frozen schema verbatim:
``agora_kb/schema/templates/kb_schema.md`` for KB wiki schema 1 (ADR-0010) and
``agora_kb/schema/templates/kb_schema_v2.md`` for KB wiki schema 2 (ADR-0041 — directory is the
KIND, the subject lives in ``subjects:``).

**Which doc is emitted is a function of the repo's ``schema_version`` and nothing else.** The
schema doc is what the curator brain reads before every INGEST (it is copied verbatim into the run
bundle as ``bundle/schema.md``), so emitting the wrong version is emitting the wrong contract to
the model — hence the selection is keyed on the SAME canonical value lint reads
(``_meta/taxonomy.yaml: schema_version``, ADR-0010 §5.1) rather than on a separate flag that could
drift from it. Both templates keep the same section numbering (§0…§9) precisely so a prompt or a
reviewer that cites a section keeps working across the flip.

.. important::
   **Emit is the repo-init / admin path — it is NOT a curator INGEST write.** The INGEST
   curator-writable allowlist (ADR-0011 §4.0 / ADR-0010 L1-9) is exactly
   ``{ wiki/** , index.md , <domain>-moc.md , log.md , assets/** }`` and EXCLUDES ``_meta/``,
   ``_templates/``, and the schema doc + its symlinks. Those paths are written ONLY here, at repo
   init or on a separate human/admin taxonomy-evolution path — never by the sandboxed curator brain
   during a consolidation run. ``_meta/taxonomy.yaml`` is consequently a FIXED, READ-ONLY input to
   every INGEST run (ADR-0010 D6): the brain reads ``allowed_tags`` / ``domains`` but can never add
   to either.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from agora_kb.core.atomicio import atomic_write_text
from agora_kb.core.layout import RepoLayout

__all__ = ["Taxonomy", "emit_schema"]

# The schema doc's canonical basename and its symlink aliases (ADR-0010 §1 / §4). All four agent
# guides resolve to the same emitted schema doc, so any tool that reads CLAUDE/QWEN/GEMINI sees the
# identical rules.
_SCHEMA_DOC_NAME = "AGENTS.md"
_SYMLINK_NAMES = ("CLAUDE.md", "QWEN.md", "GEMINI.md")

# Packaged template locations under agora_kb.schema/templates/, keyed by KB wiki schema version.
# Schema 1 is ADR-0010's frozen doc, UNCHANGED; schema 2 is ADR-0041's. A version that is not a key
# here has no emittable contract, and emit REFUSES rather than shipping a doc that describes a
# different schema than the repo declares (see :func:`_read_schema_template`).
_TEMPLATE_PKG = "agora_kb.schema"
_SCHEMA_TEMPLATES: dict[int, tuple[str, str]] = {
    1: ("templates", "kb_schema.md"),
    2: ("templates", "kb_schema_v2.md"),
}


class Taxonomy(BaseModel):
    """The closed tag/domain vocabulary written to ``_meta/taxonomy.yaml`` (ADR-0010 §5).

    Matches the ``_meta/taxonomy.yaml`` shape: ``schema_version`` (canonical single source of
    truth, §5.1), ``taxonomy_policy`` governing the SEPARATE admin evolution path (``open |
    review-only | capped:<N>``, §5.2 — not consulted during INGEST), ``allowed_tags`` (the closed
    set of kebab-case tag keys), and ``domains`` (the closed set of allowed domains). Tuples keep
    the model immutable
    and the emitted YAML deterministically ordered. ``extra='forbid'`` rejects stray keys so the
    on-disk taxonomy can never drift into an unrecognized shape.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    taxonomy_policy: str = "open"
    allowed_tags: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()


# A theme template (one idea per note; cites raw/) and a daily template (the dated consolidation
# journal). One template per type is the ADR-0010 §4 ``_templates/`` contract; theme + daily are the
# two the curator most relies on (index/moc are created/extended structurally by APPLY).
_THEME_TEMPLATE = """\
---
title: <human title>
type: theme
aliases: []
tags: []
created: <run_date>
updated: <run_date>
status: active
summary: <one-line precis>
sources: []
related: []
confidence: high
---

<!-- agora:body:start id=<candidate_id> -->
<one atomic idea, with the context it needs, citing its raw/ source.[^1]>

[^1]: raw/<domain>/<source>
<!-- agora:body:end id=<candidate_id> -->
"""

_DAILY_TEMPLATE = """\
---
title: <human title>
type: daily
aliases: []
tags: []
created: <run_date>
updated: <run_date>
status: active
summary: <one-line precis of the run's consolidation>
date: <run_date>
run_id: <run_id>
sources: []
---

## <run_date>

<!-- agora:body:start id=<candidate_id> -->
<what was consolidated this run; link into the themes it fed, e.g. [[some-theme]].>
<!-- agora:body:end id=<candidate_id> -->
"""

# KB wiki schema 2 (ADR-0041 D1): ``_templates/`` holds one template per KIND, not per type. The
# two that have a day-1 producer are emitted — ``concept`` (the successor to ``theme``) and ``note``
# (the successor to ``daily``, now ONE journal per run_date at wiki/notes/<yyyy>/<mm>/, D2.6).
# ``summary`` and ``entity`` are deliberately absent: those tiers SHIP EMPTY with no producer
# (OD-7 / OD-8), and shipping a template for a note nothing may create would read as an invitation.
_CONCEPT_TEMPLATE = """\
---
title: <human title>
kind: concept
kb: <kb_id>
subjects: []
aliases: []
tags: []
created: <run_date>
updated: <run_date>
status: active
summary: <one-line precis>
sources: []
related: []
confidence: high
---

<!-- agora:body:start id=<candidate_id> -->
<one atomic idea, with the context it needs, citing its raw/ source.[^1]>

[^1]: raw/<domain>/<source>
<!-- agora:body:end id=<candidate_id> -->
"""

_NOTE_TEMPLATE = """\
---
title: <human title>
kind: note
kb: <kb_id>
subjects: []
aliases: []
tags: []
created: <run_date>
updated: <run_date>
status: active
summary: <one-line precis of the run's consolidation>
date: <run_date>
run_id: <run_id>
sources: []
---

## <run_date> · <domain>

<!-- agora:body:start id=<candidate_id> -->
<what was consolidated this run; link into the concepts it fed, e.g. [[some-concept]].>
<!-- agora:body:end id=<candidate_id> -->
"""

# Per-version ``_templates/`` sets. Schema 1's pair is byte-identical to every release before
# ADR-0041; nothing in ``src/`` reads these files, so they are documentation for the human editing
# by hand — which is exactly why they must not describe a layout the repo is not on.
_TEMPLATES_BY_VERSION: dict[int, dict[str, str]] = {
    1: {
        "theme.md": _THEME_TEMPLATE,
        "daily.md": _DAILY_TEMPLATE,
    },
    2: {
        "concept.md": _CONCEPT_TEMPLATE,
        "note.md": _NOTE_TEMPLATE,
    },
}


# The header placeholder substituted with the canonical schema_version on emit, so the schema-doc
# header ALWAYS equals the emitted taxonomy and L1-17 never rejects a freshly-emitted repo (ADR-0010
# §5.1). Substituted by exact-token replace (NOT str.format) because the template body contains
# literal ``{ ... }`` YAML braces that str.format would misinterpret.
_SCHEMA_VERSION_PLACEHOLDER = "{schema_version}"


def _read_schema_template(schema_version: int) -> str:
    """Return the packaged KB schema-doc template for ``schema_version``, header substituted.

    Two jobs, and they are the same lookup on purpose. (1) SELECTION: ``schema_version`` picks the
    template — ADR-0010's frozen v1 doc or ADR-0041's v2 doc. (2) SUBSTITUTION: the chosen
    template's header carries a ``{schema_version}`` placeholder, replaced with the canonical value
    so the emitted doc's header equals ``_meta/taxonomy.yaml: schema_version`` (ADR-0010 §5.1).
    Without the substitution a bumped ``Taxonomy.schema_version`` would emit a header still saying
    ``1`` and the very next lint would fail L1-17 (schema-doc-header drift).

    A version with no packaged template raises :class:`ValueError` rather than falling back to v1.
    A silent fallback is the one failure mode worth refusing outright: the emitted doc is copied
    verbatim into the curator's run bundle, so shipping the v1 contract into a repo that declares a
    different schema would hand the brain rules for a layout that does not exist — and the resulting
    plan failures would be attributed to the model, not to the emit.
    """
    location = _SCHEMA_TEMPLATES.get(schema_version)
    if location is None:
        raise ValueError(
            f"no packaged KB schema doc for schema_version {schema_version!r}; "
            f"emittable versions are {sorted(_SCHEMA_TEMPLATES)}"
        )
    files = resources.files(_TEMPLATE_PKG)
    text = files.joinpath(*location).read_text(encoding="utf-8")
    return text.replace(_SCHEMA_VERSION_PLACEHOLDER, str(schema_version))


def _write_text(dest: Path, text: str, *, force: bool) -> None:
    """Write ``text`` to ``dest`` durably; idempotently skip an existing file unless ``force``.

    The occupancy guard tests ``dest.exists() or dest.is_symlink()`` so a DANGLING symlink at
    ``dest`` is also detected: ``exists()`` follows symlinks and is False for a dangling one, which
    would let ``atomic_write_text(exclusive=True)`` raise ``FileExistsError`` on the ``os.link`` of
    the path entry. Mirrors :func:`_link_or_copy`'s guard so the two writers are consistent on the
    admin/force re-emit path.
    """
    if dest.exists() or dest.is_symlink():
        if not force:
            return
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(dest, text, exclusive=True)


def _link_or_copy(symlink_path: Path, target_name: str, schema_text: str, *, force: bool) -> None:
    """Create ``symlink_path`` pointing at ``target_name`` (``AGENTS.md``), idempotent unless force.

    Symlinks are relative (``AGENTS.md``, not an absolute path) so the repo stays portable when
    moved or cloned. **Windows caveat:** ``os.symlink`` requires Developer Mode or elevation on
    Windows; if symlink creation is not permitted (``OSError``), we fall back to writing a plain
    copy of the schema doc so ``CLAUDE.md`` / ``QWEN.md`` / ``GEMINI.md`` still resolve to the same
    content. The parser treats both forms as parse-exempt: real symlinks are skipped by symlink
    identity and plain copies by exact basename (ADR-0010 §1), so neither is ever parsed as a note.
    """
    if symlink_path.exists() or symlink_path.is_symlink():
        if not force:
            return
        symlink_path.unlink()
    symlink_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target_name, symlink_path)
    except OSError:
        # Windows without symlink privilege (or a filesystem that forbids symlinks): emit a plain
        # copy so the agent guide still resolves to the schema doc's content.
        atomic_write_text(symlink_path, schema_text, exclusive=True)


def emit_schema(
    layout: RepoLayout,
    *,
    taxonomy: Taxonomy | None = None,
    schema_version: int | None = None,
    force: bool = False,
) -> None:
    """Emit the editorial schema into the repo at ``layout`` (repo-init / admin path; NOT INGEST).

    Writes, idempotently (existing files are left untouched unless ``force=True``):

    * ``AGENTS.md`` — the schema doc, from the packaged template ``templates/kb_schema.md``;
    * ``CLAUDE.md`` / ``QWEN.md`` / ``GEMINI.md`` — relative symlinks to ``AGENTS.md`` (with a
      plain-copy fallback where symlinks are unavailable — see the Windows caveat on
      :func:`_link_or_copy`);
    * ``_meta/taxonomy.yaml`` — ``yaml.safe_dump`` of ``taxonomy`` (default :class:`Taxonomy`), the
      FIXED read-only INGEST input (ADR-0010 D6 / §5);
    * ``_templates/`` — the note templates for the emitted schema: ``theme.md`` + ``daily.md``
      (one per TYPE) on schema 1, ``concept.md`` + ``note.md`` (one per KIND, ADR-0041 D1) on
      schema 2.

    ``schema_version`` selects WHICH schema is emitted — the doc and the ``_templates/`` set. It
    defaults to ``taxonomy.schema_version``, which is the canonical value (ADR-0010 §5.1), so the
    normal call passes only a taxonomy and cannot produce a doc that disagrees with it. Passing a
    value that CONTRADICTS the taxonomy raises :class:`ValueError`: emit is the one writer of both
    the header and the canonical file, so manufacturing the exact drift L1-17 exists to catch would
    be emit writing its own bug report. The parameter is therefore a redundant, checked statement of
    intent for a caller that knows the version out of band (a converter writing a destination repo,
    a test), never an override.

    ``_meta/`` and ``_templates/`` are deliberately OUTSIDE the INGEST curator-writable allowlist
    (ADR-0011 §4.0): they are written here at repo init or on the admin path only. Re-running with
    the same arguments is a no-op; pass ``force=True`` to overwrite an existing repo's schema /
    taxonomy / templates (e.g. a schema-version bump on the admin path, which now keeps the
    schema-doc header in sync with the taxonomy so L1-17 passes on a fresh emit).

    Emit writes ONLY the off-allowlist schema / taxonomy / templates; the on-allowlist root
    ``index.md`` (a NOTE) is created by the separate curator/init path, NOT here — so a
    freshly-emitted repo legitimately has zero notes (lint passes vacuously).
    """
    tax = taxonomy if taxonomy is not None else Taxonomy()
    if schema_version is not None and schema_version != tax.schema_version:
        raise ValueError(
            f"schema_version={schema_version!r} contradicts taxonomy.schema_version="
            f"{tax.schema_version!r}; emit writes BOTH the canonical _meta/taxonomy.yaml and the "
            f"schema-doc header, so it must not manufacture the drift lint L1-17 rejects "
            f"(ADR-0010 §5.1)"
        )
    version = tax.schema_version

    # Render the schema doc from the packaged template for `version`, with the canonical
    # schema_version substituted into its header, so the header equals _meta/taxonomy.yaml and
    # L1-17 passes on a fresh emit even for an admin schema-version bump (ADR-0010 §5.1).
    schema_text = _read_schema_template(version)
    _write_text(layout.schema_file, schema_text, force=force)

    for name in _SYMLINK_NAMES:
        _link_or_copy(layout.root / name, _SCHEMA_DOC_NAME, schema_text, force=force)

    # _meta/taxonomy.yaml — deterministic key order matching the §5 documented shape. Tuples are
    # converted to plain lists so safe_dump renders block-style YAML lists.
    taxonomy_doc: dict[str, object] = {
        "schema_version": tax.schema_version,
        "taxonomy_policy": tax.taxonomy_policy,
        "domains": list(tax.domains),
        "allowed_tags": {t: {} for t in tax.allowed_tags},
    }
    taxonomy_yaml = yaml.safe_dump(taxonomy_doc, sort_keys=False, allow_unicode=True)
    meta_dir = layout.root / "_meta"
    _write_text(meta_dir / "taxonomy.yaml", taxonomy_yaml, force=force)

    templates_dir = layout.root / "_templates"
    for filename, text in _TEMPLATES_BY_VERSION[version].items():
        _write_text(templates_dir / filename, text, force=force)
