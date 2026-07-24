"""Canonical curator INGEST constants — the ONE allowlist + tuning defaults (ADR-0011 §4.0).

Defined ONCE here and referenced by every gate (the §4.1 PLAN PATH/ALLOWLIST check in
:mod:`agora_kb.curator.plan` and the §4.3/§4.5 final-diff assertion in
:mod:`agora_kb.curator.worker`) so the integrity boundary has a single source of truth and the two
checks can never drift (ADR-0011 §4.0: "defined once in code (``curator/constants.py``) and
referenced by every check below and by the final-diff assertion").

The canonical allowlist (ADR-0011 §4.0 / ADR-0008 §4 verbatim):
``ALLOWLIST = { wiki/** , index.md , <domain>-moc.md , log.md , assets/** }``. ``<domain>-moc.md``
lives under ``wiki/`` already, so the path predicate reduces to: an exact match of one of the
top-level files, or a path under one of the allowed directory prefixes. ``_agora_scratch/`` (the
backend's writable scratch, §3) is git-ignored in the worktree's own ``.gitignore`` and must produce
ZERO tracked changes — it is NOT in the allowlist.
"""

from __future__ import annotations

# The exact top-level files an INGEST run's curated diff may touch (ADR-0011 §4.0).
ALLOWLIST_FILES: frozenset[str] = frozenset({"index.md", "log.md"})

# Directory prefixes (POSIX, trailing slash) under which any added/modified path is allowed. A
# ``<domain>-moc.md`` is under ``wiki/`` so it is covered by the ``wiki/`` prefix.
ALLOWLIST_DIR_PREFIXES: tuple[str, ...] = ("wiki/", "assets/")

# The git-ignored backend scratch dir (ADR-0011 §3 / §4.3): writable inside the worktree mount but
# allowlist-EXCLUDED — the final-diff assertion requires it to produce ZERO tracked changes.
SCRATCH_DIRNAME = "_agora_scratch"

# The immutable schema-doc symlinks (ADR-0011 §4.5): they may EXIST unchanged at base_commit, but
# ANY add/modify/delete/rename touching them FAILS the run, and they are never in the allowlist.
SCHEMA_SYMLINKS: frozenset[str] = frozenset({"CLAUDE.md", "QWEN.md", "GEMINI.md"})

# §5.1 default retry budget: an event reaching this many distinct manifests/error records is sent
# terminal to ``failed/`` rather than returned to ``inbox/`` (``repo.yaml curator.max_attempts``,
# default 3). Pinned here until a structured repo.yaml curator-config reader lands.
DEFAULT_MAX_ATTEMPTS = 3

# §1.3 default PASS-2 body byte ceiling surfaced to the model as the ``{n_bytes}`` prompt hint
# (``repo.yaml curator.limits.body_byte_bound``). The worker's §4.2 check is the AUTHORITATIVE
# enforcement; this only bounds the hint. Default; operator tuning via load_repo_config.
DEFAULT_BODY_BYTE_BOUND = 8192

# §1.3 default related-notes retrieval breadth for the bundle — the per-candidate
# ``wiki.query(limit=…)`` fan-out (``repo.yaml curator.related_k``). Default; tuning via config.
DEFAULT_RELATED_K = 8

# §1.3 default per-run candidate cap (``repo.yaml curator.limits.max_candidates_per_run``): the
# FIFO claim caps the snapshot at this many DISTINCT tier-2 content groups (post-dedup candidates,
# the unit PASS-1 adjudicates) so one harvest surge can never build an unbounded plan prompt; the
# remainder stays in the inbox for the next trigger (ADR-0024 OD-3a, #60). 32 is the documented
# contract default (frontier-model sized); small local models want lower (≤8B: 8-12, 30B-A3B:
# 16-24 — INGEST-CONTRACT §1.3). Default; operator tuning via load_repo_config.
DEFAULT_MAX_CANDIDATES_PER_RUN = 32


def is_allowlisted_path(rel_path: str) -> bool:
    """True iff ``rel_path`` (POSIX, repo-relative) is within the canonical §4.0 ALLOWLIST.

    A path is allowed iff it is one of :data:`ALLOWLIST_FILES` exactly, or it lives under one of
    :data:`ALLOWLIST_DIR_PREFIXES`. Everything else (``_kb/``, ``_meta/``, ``_templates/``,
    ``raw/``, git internals, hooks, the schema doc + its symlinks, ``_agora_scratch/``) is rejected
    — the final-diff assertion fails the whole run on any such add/modify/rename (ADR-0011 §4.0).
    """
    if rel_path in ALLOWLIST_FILES:
        return True
    return any(rel_path.startswith(prefix) for prefix in ALLOWLIST_DIR_PREFIXES)
