"""Tests for the ADR-0012 §2/§9 derived reader cache (issue #26).

The load-bearing contract is BYTE-IDENTICAL query output whether the cache is absent or present. The
pure-Python scan stays the oracle; the cache only skips re-parsing unchanged files and prunes the
scoring loop via the exact in-memory inverted index (the ADR-0012 §9 FTS5/ripgrep candidate
accelerators are deferred to a load-avoiding reader — issue #28).

The fixture is a git-initialized repo (``branch_commit()`` must resolve to key the cache) carrying
the ADR-0012 §10 corpus in the KB wiki schema-2 kind-first layout (ADR-0041 D1: ``wiki/maps/`` +
``wiki/concepts/``) PLUS a basename-derived-title note and an abutting-links note whose only
lexical token is TOKENIZER-SYNTHESIZED — cases a raw-bytes accelerator would miss but the exact
inverted index (fed the tokenizer output) returns, guarding against the parity class the adversarial
review found in the deferred ripgrep prefilter.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agora_kb.config import ConfigError, IndexPolicy, load_index_policy
from agora_kb.core import index_cache
from agora_kb.core.hashing import content_sha256
from agora_kb.core.layout import InvalidWriterError, RepoLayout
from agora_kb.core.repo import Repo
from agora_kb.core.wiki import Wiki, build_cache

WHEN = datetime(2026, 6, 12, 0, 0, 0, tzinfo=UTC)

INDEX_MD = "# personal\n\n- [AI Tech MOC](wiki/maps/ai-tech.md)\n"
# KB wiki schema 2 (ADR-0041 D1/D5): the map is recognised by its DIRECTORY and its subject scope
# by its `subjects:` frontmatter — never by the filename or a path segment.
MOC = (
    "---\nstatus: active\nkind: map\nsubjects: [ai-tech]\n---\n# AI Tech\n\n"
    "- [Curator concurrency](../concepts/curator-concurrency.md) — single-writer curator\n"
    "- [Inbox design](../concepts/inbox-design.md) — append-only per-writer inbox\n"
)
CURATOR_CONCURRENCY = (
    "---\nstatus: active\ntags: [single-writer, concurrency]\n---\n# Curator Concurrency\n\n"
    "The curator acquires a per-repo flock on curator.lock so exactly one writer advances the "
    "curated branch.\n\n## Compare and swap\nConcurrency control is enforced by compare-and-swap.\n"
)
INBOX_DESIGN = (
    "---\nstatus: active\ntags: [inbox, append-only]\n---\n# Inbox Design\n\n"
    "The inbox is append-only and per-writer namespaced.\n"
)
# A note with NO H1 and NO frontmatter title: its title tokens come from the BASENAME
# ("zephyrquux topic"), which are ABSENT from the file text — proves the exact inverted index
# (built from field_tokens) returns it even though a raw-bytes accelerator could not.
BASENAME_TITLE_NOTE = "---\nstatus: active\n---\n\nSome body prose without the rare term.\n"

# An orphan note with ABUTTING links: _strip_link_punctuation concatenates the labels into the
# SYNTHESIZED body token "zebraquux" (no literal substring in the file) — proves the inverted index
# (fed the tokenizer OUTPUT) is exact where a raw-bytes prefilter would under-approximate (the class
# of bug the adversarial review found in the now-deferred ripgrep prefilter).
SYNTH_TOKEN_NOTE = "---\nstatus: active\n---\n# Synth\n\nSee [zebra](x.md)[quux](y.md) here.\n"

# Query battery: a hit, a not_found, a heading match, a lexical match, a basename-title term, and a
# tokenizer-synthesized term.
QUERIES = [
    "curator concurrency control",
    "quantum biology photosynthesis",
    "compare swap",
    "inbox append-only",
    "zephyrquux",
    "zebraquux",
]


def _git(root: Path, *args: str) -> None:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    import os

    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, env={**os.environ, **env}
    )


def _write_corpus(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text(INDEX_MD, encoding="utf-8")
    concepts = root / "wiki" / "concepts"
    concepts.mkdir(parents=True, exist_ok=True)
    (root / "wiki" / "maps").mkdir(parents=True, exist_ok=True)
    (root / "wiki" / "maps" / "ai-tech.md").write_text(MOC, encoding="utf-8")
    (concepts / "curator-concurrency.md").write_text(CURATOR_CONCURRENCY, encoding="utf-8")
    (concepts / "inbox-design.md").write_text(INBOX_DESIGN, encoding="utf-8")
    (concepts / "zephyrquux-topic.md").write_text(BASENAME_TITLE_NOTE, encoding="utf-8")
    (concepts / "synth-token.md").write_text(SYNTH_TOKEN_NOTE, encoding="utf-8")


def _repo(tmp_path: Path, *, name: str = "personal") -> Repo:
    """A git-initialized repo carrying the corpus, committed on the curated branch ``main``."""
    root = tmp_path / name
    _write_corpus(root)
    (root / ".gitignore").write_text("_kb/\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    return Repo.resolve(root)


def _results(layout: RepoLayout) -> dict:
    return {q: Wiki(layout).query(q) for q in QUERIES}


def _set_index_policy(root: Path, **kv: str) -> None:
    (root / "_kb").mkdir(exist_ok=True)
    body = "index:\n" + "".join(f"  {k}: {v}\n" for k, v in kv.items())
    (root / "_kb" / "repo.yaml").write_text(body, encoding="utf-8")


# --- digest choice (D1): source_digest is a strict refinement of content_sha256 ------------------


def test_source_digest_distinguishes_bytes_that_content_sha256_collapses() -> None:
    """The per-file gate MUST use source_digest, not content_sha256 (ADR-0012 §2 as-built note).

    ``content_sha256`` normalizes CRLF→LF, per-line trailing whitespace, and NFC — so two
    byte-divergent parser inputs collapse to one hash and a cache keyed on it would reuse a stale
    parse. ``source_digest`` digests the exact bytes, so any parse-affecting difference re-parses.
    """
    lf, crlf = "# Title\nbody line\n", "# Title\r\nbody line\r\n"
    assert content_sha256(lf) == content_sha256(crlf)  # normalization collapses them
    assert index_cache.source_digest(lf) != index_cache.source_digest(
        crlf
    )  # exact-bytes distinguishes

    import unicodedata

    nfc = "café\n"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    assert content_sha256(nfc) == content_sha256(nfd)
    assert index_cache.source_digest(nfc) != index_cache.source_digest(nfd)


# --- serialization + read_payload robustness -----------------------------------------------------


def test_serialize_payload_is_deterministic_and_sorted() -> None:
    payload = index_cache.CachePayload(
        cache_schema_version=index_cache.CACHE_SCHEMA_VERSION,
        curated_commit="abc",
        notes={"b.md": {"sha": "1", "note": {}}, "a.md": {"sha": "2", "note": {}}},
    )
    text = index_cache.serialize_payload(payload)
    assert text == index_cache.serialize_payload(payload)  # stable
    assert text.endswith("\n")
    # keys are sorted (a.md before b.md; top-level keys sorted)
    assert text.index('"a.md"') < text.index('"b.md"')
    assert text.index("cache_schema_version") < text.index("curated_commit") < text.index('"notes"')


def test_read_payload_none_on_absent_corrupt_and_schema_mismatch(tmp_path: Path) -> None:
    p = tmp_path / "x.notes.json"
    assert index_cache.read_payload(p) is None  # absent
    p.write_text("{ not json", encoding="utf-8")
    assert index_cache.read_payload(p) is None  # corrupt
    p.write_text(json.dumps({"cache_schema_version": 999, "curated_commit": "a", "notes": {}}))
    assert index_cache.read_payload(p) is None  # schema mismatch
    current = index_cache.CACHE_SCHEMA_VERSION  # keeps these probes on the SHAPE checks (a stale
    # literal version would short-circuit at the version gate and make them vacuous)
    p.write_text(json.dumps({"cache_schema_version": current, "curated_commit": "a", "notes": []}))
    assert index_cache.read_payload(p) is None  # notes must be a dict
    p.write_text(
        json.dumps(
            {"cache_schema_version": current, "curated_commit": "a", "notes": {"n.md": {"sha": 1}}}
        )
    )
    assert index_cache.read_payload(p) is None  # malformed entry (sha not str / no note)


# --- the core parity contract --------------------------------------------------------------------


def test_cached_query_byte_identical_to_uncached(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    layout = repo.layout
    baseline = _results(layout)  # cache absent → uncached scan
    assert not layout.index_notes_path().exists(), "the read path must NEVER write the cache"

    build_cache(repo)
    assert layout.index_notes_path().is_file()
    cached = _results(layout)
    for q in QUERIES:
        assert cached[q] == baseline[q], f"cache changed query output for {q!r}"

    # the basename-title note must actually be RETURNED (else the parity above is vacuous for it)
    zq = baseline["zephyrquux"]
    assert zq.status == "ok"
    assert any(h.path.endswith("zephyrquux-topic.md") for h in zq.hits)


def test_rebuild_is_byte_identical_same_and_cross_clone(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    build_cache(repo)
    first = repo.layout.index_notes_path().read_bytes()
    build_cache(repo)
    assert repo.layout.index_notes_path().read_bytes() == first  # same-dir rebuild stable

    clone = tmp_path / "clone"
    _git(repo.root, "clone", str(repo.root), str(clone))
    clone_repo = Repo.resolve(clone)
    build_cache(clone_repo)
    # different mtimes (fresh checkout), identical committed bytes → identical cache (invariant #1)
    assert clone_repo.layout.index_notes_path().read_bytes() == first


def test_inverted_prefilter_is_exact_for_synthesized_and_basename_tokens(tmp_path: Path) -> None:
    """The candidate prefilter must return notes matched only by a TOKENIZER-SYNTHESIZED token or a
    BASENAME-derived title token — the exactness a raw-bytes accelerator lacks (the class of parity
    bug the adversarial review found in the deferred ripgrep prefilter). Guarded via cache parity:
    build the cache (uses the prefilter) and assert it equals the uncached scan AND returns the
    two notes whose only lexical evidence is such a token.
    """
    repo = _repo(tmp_path)
    baseline = _results(repo.layout)  # uncached oracle

    # "zebraquux" exists ONLY as a synthesized body token (abutting link labels), not as file bytes;
    # "zephyrquux" exists ONLY as a basename-derived title token — neither is a literal substring.
    for q, note in (("zebraquux", "synth-token.md"), ("zephyrquux", "zephyrquux-topic.md")):
        assert baseline[q].status == "ok", f"{q!r} should be found by the exact scan"
        assert any(h.path.endswith(note) for h in baseline[q].hits)

    build_cache(repo)
    cached = _results(repo.layout)
    for q in QUERIES:
        assert cached[q] == baseline[q], f"prefilter changed query output for {q!r}"


# --- freshness gates -----------------------------------------------------------------------------


def test_stale_commit_falls_back_to_scan(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    build_cache(repo)
    # add + commit a NEW note without rebuilding the cache → curated_commit mismatch → scan.
    new = repo.root / "wiki" / "concepts" / "raftlog-note.md"
    new.write_text("---\nstatus: active\n---\n# Raftlog Note\n\nThe raftlog quorum detail.\n")
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-m", "add note")
    result = Wiki(repo.layout).query("raftlog quorum")
    assert result.status == "ok"
    assert any(h.path.endswith("raftlog-note.md") for h in result.hits)


def test_schema_version_bump_invalidates_cache(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    baseline = Wiki(repo.layout).query("curator concurrency control")  # same-repo uncached oracle
    build_cache(repo)
    # Rewrite the payload with a stale schema version; read_payload must reject it → scan-correct.
    path = repo.layout.index_notes_path()
    doc = json.loads(path.read_text())
    doc["cache_schema_version"] = index_cache.CACHE_SCHEMA_VERSION + 1
    path.write_text(json.dumps(doc))
    assert index_cache.read_payload(path) is None
    assert Wiki(repo.layout).query("curator concurrency control") == baseline


def test_uncommitted_edit_self_corrects_via_source_digest(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    build_cache(repo)  # cache stamped at the committed tip
    # Edit a note ON DISK without committing: the whole-cache commit gate still matches, but the
    # per-file source_digest differs → that ONE file is re-parsed, so the edit is reflected.
    note = repo.root / "wiki" / "concepts" / "inbox-design.md"
    note.write_text(
        "---\nstatus: active\ntags: [inbox]\n---\n# Inbox Design\n\nNew snorquux term.\n"
    )
    result = Wiki(repo.layout).query("snorquux term")
    assert result.status == "ok"
    assert any(h.path.endswith("inbox-design.md") for h in result.hits)


# --- robustness / crash-proof fallbacks ----------------------------------------------------------


def test_corrupt_cache_falls_back_and_read_path_never_writes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    build_cache(repo)
    path = repo.layout.index_notes_path()
    path.write_text("garbage not json", encoding="utf-8")
    before = path.read_bytes()
    result = Wiki(repo.layout).query("curator concurrency control")
    assert result.status == "ok"  # degraded to scan, no crash
    assert path.read_bytes() == before, "the read path must not rewrite/repair the cache"


def test_corrupt_inner_note_reparses_not_crashes(tmp_path: Path) -> None:
    """A structurally-corrupt cached inner ``note`` (valid ENVELOPE, matching sha) must NOT crash
    kb_query — the reuse path re-parses that one file (finding-6 guard). Covers both a bogus dict
    (KeyError in _note_from_dict) AND an INCOMPLETE field_tokens (valid until the scorer reads a
    missing field) — the case that survived construction and crashed later before the strict check.
    """
    repo = _repo(tmp_path)
    baseline = Wiki(repo.layout).query("curator concurrency control")  # same-repo uncached oracle
    build_cache(repo)
    path = repo.layout.index_notes_path()
    target = "wiki/concepts/curator-concurrency.md"  # the note the query hits
    pristine_text = path.read_text(encoding="utf-8")
    valid_note = json.loads(pristine_text)["notes"][target]["note"]

    corruptions = [
        {"bogus": 1},  # missing every key → KeyError inside _note_from_dict
        {
            **valid_note,
            "field_tokens": {"title": ["curator"]},
        },  # INCOMPLETE field_tokens → ValueError
    ]
    for corruption in corruptions:
        doc = json.loads(pristine_text)  # start from a fresh, valid cache each time
        doc["notes"][target]["note"] = corruption  # keep the matching sha → reuse branch is taken
        path.write_text(json.dumps(doc), encoding="utf-8")
        result = Wiki(repo.layout).query("curator concurrency control")
        assert result == baseline, f"corrupt inner note {list(corruption)[:1]} not self-healed"
        assert any(h.path == target for h in result.hits)


def test_empty_repo_not_found_with_cache_enabled(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "placeholder").write_text("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "empty")
    result = Wiki(RepoLayout(root)).query("anything")
    assert result.status == "not_found"


def test_disabled_policy_uses_uncached_scan(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    baseline = Wiki(repo.layout).query("curator concurrency control")  # same-repo uncached oracle
    build_cache(repo)
    _set_index_policy(repo.root, enabled="false")
    # even with a cache present, enabled=false must produce identical (scan) output
    assert Wiki(repo.layout).query("curator concurrency control") == baseline


def test_query_order_invariant_to_scan_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    build_cache(repo)
    normal = Wiki(repo.layout).query("curator concurrency control")

    # Reverse the rglob order the loader sees; the sorted() in _iter_note_files + recomputed
    # indeg must make the output identical (indeg is never read from the cache).
    real_rglob = Path.rglob

    def _reversed_rglob(self: Path, pat: str):
        return list(reversed(list(real_rglob(self, pat))))

    monkeypatch.setattr(Path, "rglob", _reversed_rglob)
    assert Wiki(repo.layout).query("curator concurrency control") == normal


# --- the worker/CLI writer helper ----------------------------------------------------------------


def test_rebuild_index_cache_helper_enabled_disabled_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agora_kb.curator import worker

    repo = _repo(tmp_path)
    # enabled (default) → builds, returns True
    assert worker.rebuild_index_cache(repo) is True
    assert repo.layout.index_notes_path().is_file()

    # disabled → returns True (nothing to flag), does not error
    repo.layout.index_notes_path().unlink()
    _set_index_policy(repo.root, enabled="false")
    assert worker.rebuild_index_cache(repo) is True
    assert not repo.layout.index_notes_path().exists()

    # genuine failure → swallowed, returns False (caller flags index_cache_unbuilt)
    _set_index_policy(repo.root, enabled="true")

    def _boom(_repo):  # noqa: ANN001
        raise OSError("disk full")

    monkeypatch.setattr("agora_kb.core.wiki.build_cache", _boom)
    assert worker.rebuild_index_cache(repo) is False


# --- config + layout guards ----------------------------------------------------------------------


def test_load_index_policy_defaults_and_validation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    layout = repo.layout
    assert load_index_policy(layout) == IndexPolicy(enabled=True)
    # a non-boolean enabled must surface loudly (never silently take the default)
    (repo.root / "_kb").mkdir(exist_ok=True)
    (repo.root / "_kb" / "repo.yaml").write_text("index:\n  enabled: notabool\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_index_policy(layout)


def test_index_paths_are_guarded_and_under_kb_index(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path / "myrepo")
    assert layout.index_notes_path() == layout.kb_dir / "index" / "myrepo.notes.json"
    assert layout.index_cache_dir == layout.kb_dir / "index"
    # explicit repo arg is validated as a safe path component
    with pytest.raises(InvalidWriterError) as excinfo:
        layout.index_notes_path("../escape")
    # …and the remedy follows the INPUT: the directory here is fine ("myrepo"), so telling the
    # operator to rename it would be a misdirection — the caller's stem is what has to change.
    message = str(excinfo.value)
    assert "cache stem" in message and "rename the repo directory" not in message


# --- CLI ------------------------------------------------------------------------------------------


def test_cli_index_build_status_clear(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from agora_kb.cli import main

    repo = _repo(tmp_path)
    root = str(repo.root)

    assert main(["index", "build", "--repo", root]) == 0
    out = capsys.readouterr().out
    assert "built" in out and repo.layout.index_notes_path().is_file()

    assert main(["index", "status", "--repo", root]) == 0
    out = capsys.readouterr().out
    assert "FRESH" in out

    assert main(["index", "clear", "--repo", root]) == 0
    out = capsys.readouterr().out
    assert "cleared" in out and not repo.layout.index_notes_path().exists()


def test_cli_doctor_reports_index_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from agora_kb.cli import main

    repo = _repo(tmp_path)
    build_cache(repo)
    main(["doctor", "--repo", str(repo.root)])
    out = capsys.readouterr().out
    assert "index:" in out and "cache=fresh" in out


# --- unsafe repo DIRECTORY names: the write path degrades like the read path (issue #108) --------


@pytest.mark.parametrize("name", ["My Knowledge"])
def test_unsafe_repo_dir_name_never_tracebacks_and_reports_consistently(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """A repo DIRECTORY name that is not a safe filename component must never crash a build (#108).

    ``~/My Knowledge`` is an ordinary directory that ``agora repo init`` accepts, but it cannot
    address ``_kb/index/<repo>.notes.json`` — whitespace is outside the pathsafe allowlist AND
    outside the legacy writer charset, so both halves of the union predicate refuse it. The five
    READ call sites have always degraded to a full scan; the WRITE path (``build_cache``) was
    unguarded and ``agora index build`` exited with a raw ``InvalidWriterError`` traceback. No
    platform branch is involved — this reproduces on POSIX and Windows alike.

    ``내지식`` used to be parametrized here too. It is now ACCEPTED (DRILLDOWN-169 D17, issue
    #167) and its end-to-end behaviour is pinned by
    :func:`test_non_ascii_repo_dir_name_now_addresses_a_cache` below.
    """
    from agora_kb.cli import main

    repo = _repo(tmp_path, name=name)
    root = str(repo.root)

    # The layout guard still refuses to INVENT a stem (read and write agree "there is no cache"),
    # but it now explains itself: cause + what still works + remedy.
    with pytest.raises(InvalidWriterError) as excinfo:
        repo.layout.index_notes_path()
    message = str(excinfo.value)
    assert name in message and "full scan" in message and "rename" in message
    # Every surface below interpolates this message inside its OWN parentheses, so the message may
    # not open one of its own, and it must spell the rule out rather than paste `\A[A-Za-z0-9]...`
    # in front of the operator.
    assert "(" not in message and ")" not in message
    assert "\\A" not in message and "[A-Za-z0-9]" not in message

    # build_cache raises the class every caller already handles (never a bare InvalidWriterError).
    with pytest.raises(ConfigError):
        build_cache(repo)

    # `agora index build`: no traceback, and the output says all three things.
    assert main(["index", "build", "--repo", root]) == 1
    out = capsys.readouterr().out
    assert "could not build" in out and "no reader cache was written" in out  # (1) nothing written
    assert name in out  # (2) the cause is the repo directory name
    assert "full scan" in out and "rename" in out  # (3) search still works + the remedy
    assert not repo.layout.index_cache_dir.exists()

    # The READ path is untouched: query still answers, and status/doctor agree with build that
    # there is no cache (no "built" here / "absent" there contradiction).
    result = Wiki(repo.layout).query("curator concurrency control")
    assert result.status == "ok" and result.hits
    assert Wiki(repo.layout).query("quantum biology photosynthesis").status == "not_found"

    assert main(["index", "status", "--repo", root]) == 0
    out = capsys.readouterr().out
    assert "cache: unavailable" in out and name in out

    main(["doctor", "--repo", root])
    assert "cache=absent" in capsys.readouterr().out

    # `agora index clear` is a no-op, not a crash.
    assert main(["index", "clear", "--repo", root]) == 0
    assert "no cache to clear" in capsys.readouterr().out


def test_unsafe_repo_dir_name_does_not_abort_a_curator_publish(tmp_path: Path) -> None:
    """``rebuild_index_cache`` still DEGRADES (never raises) when no cache path exists (#108).

    The translated ``ConfigError`` must keep the curator's swallow+signal posture intact: a run is
    already published in git when the rebuild runs, so an unbuildable derived cache may only be
    surfaced as ``index_cache_unbuilt``.
    """
    from agora_kb.curator.worker import rebuild_index_cache

    repo = _repo(tmp_path, name="My Knowledge")
    assert rebuild_index_cache(repo) is False
    assert not repo.layout.index_cache_dir.exists()


# --- cache-stem predicate: the union rule (DRILLDOWN-169 D17, issue #167) ------------------------

# The acceptance table measured on the pre-change build (brief §8 E-2). It is written out in full,
# both halves, because the union's WHOLE point is that neither half alone reproduces it:
#   * the legacy writer charset alone rejects every non-ASCII stem (#167 itself);
#   * ``is_safe_component`` alone rejects the Windows device stems and the trailing ``-``/``.``
#     forms, which today address real cache files — and ``core/wiki.py`` SWALLOWS the resulting
#     InvalidWriterError and falls back to a full scan, so that regression would be a silent
#     performance loss with no operator-visible error.
_STEMS_ACCEPTED_BEFORE_AND_AFTER = ["con", "CON", "nul", "com1", "aux", "foo-", "foo."]
_STEMS_NEWLY_ACCEPTED = ["내지식", "café", "a" * 130]
#   * ``-foo`` is the one spelling made only of "always accepted" ASCII characters that BOTH
#     halves still refuse (``is_safe_component`` rejects the leading ``-``; the legacy writer
#     regex requires a leading ``[A-Za-z0-9]``), so the raise message must keep saying so.
_STEMS_REFUSED = ["../escape", ".hidden", "My Knowledge", "", "-foo"]


@pytest.mark.parametrize("stem", _STEMS_ACCEPTED_BEFORE_AND_AFTER)
def test_cache_stem_keeps_every_stem_the_legacy_writer_charset_admitted(
    tmp_path: Path, stem: str
) -> None:
    """No repo that has a cache today may lose it — the union is purely ADDITIVE (D17)."""
    from agora_kb.core.pathsafe import is_safe_filename_stem

    assert is_safe_filename_stem(stem) is True
    layout = RepoLayout(tmp_path / "myrepo")
    assert layout.index_notes_path(stem) == layout.kb_dir / "index" / f"{stem}.notes.json"


@pytest.mark.parametrize("stem", _STEMS_NEWLY_ACCEPTED)
def test_cache_stem_now_admits_non_ascii_and_long_names(tmp_path: Path, stem: str) -> None:
    """Issue #167: a Unicode repo directory addresses a cache instead of silently losing one."""
    from agora_kb.core.pathsafe import is_safe_filename_stem

    assert is_safe_filename_stem(stem) is True
    layout = RepoLayout(tmp_path / "myrepo")
    assert layout.index_notes_path(stem) == layout.kb_dir / "index" / f"{stem}.notes.json"


@pytest.mark.parametrize("stem", _STEMS_REFUSED)
def test_cache_stem_still_refuses_traversal_dotfiles_and_whitespace(
    tmp_path: Path, stem: str
) -> None:
    """Both halves of the union refuse these independently — widening admitted no separator."""
    from agora_kb.core.pathsafe import is_safe_filename_stem

    assert is_safe_filename_stem(stem) is False
    with pytest.raises(InvalidWriterError):
        RepoLayout(tmp_path / "myrepo").index_notes_path(stem)


def test_cache_stem_predicate_never_invents_a_stem() -> None:
    """The predicate ANSWERS about the value as given; it must not be a slugger (D17).

    ``safe_slug_component('/etc/passwd')`` returns ``'etc-passwd'``. Routing the layout guard
    through a REWRITER would silently address a different repo's cache file, which is exactly the
    property ``test_unsafe_repo_dir_name_never_tracebacks_and_reports_consistently`` pins.
    """
    from agora_kb.core.pathsafe import is_safe_filename_stem, safe_slug_component

    assert safe_slug_component("/etc/passwd") == "etc-passwd"  # the rewriter we did NOT use
    assert is_safe_filename_stem("/etc/passwd") is False


def test_cache_stem_legacy_charset_mirror_matches_layout() -> None:
    """``pathsafe`` copies ``layout``'s writer charset (the dependency runs layout → pathsafe).

    Pinned here so the two definitions cannot drift apart unnoticed.
    """
    from agora_kb.core import layout as layout_mod
    from agora_kb.core import pathsafe

    assert pathsafe._LEGACY_WRITER_RE.pattern == layout_mod._WRITER_RE.pattern
    assert pathsafe._LEGACY_WRITER_MAX == layout_mod._WRITER_MAX


def test_non_ascii_repo_dir_name_now_addresses_a_cache(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``~/내지식`` builds, reports and clears a cache end to end (issue #167, D17).

    This is the leg flipped out of the #108 parametrize above: the directory is unchanged, only
    the stem predicate widened. ``CACHE_SCHEMA_VERSION`` is untouched (3) because only the
    FILENAME moved — the serialized ``_Note`` shape is byte-identical.
    """
    from agora_kb.cli import main

    repo = _repo(tmp_path, name="내지식")
    root = str(repo.root)

    assert repo.layout.index_notes_path() == repo.layout.kb_dir / "index" / "내지식.notes.json"

    assert main(["index", "build", "--repo", root]) == 0
    assert "built" in capsys.readouterr().out
    assert repo.layout.index_notes_path().is_file()

    payload = json.loads(repo.layout.index_notes_path().read_text(encoding="utf-8"))
    assert payload["cache_schema_version"] == index_cache.CACHE_SCHEMA_VERSION == 3

    assert main(["index", "status", "--repo", root]) == 0
    assert "FRESH" in capsys.readouterr().out

    # …and the cache is byte-identical to what the query oracle produces (the load-bearing #26
    # contract): a hit is still a hit, a miss is still a miss.
    result = Wiki(repo.layout).query("curator concurrency control")
    assert result.status == "ok" and result.hits
    assert Wiki(repo.layout).query("quantum biology photosynthesis").status == "not_found"

    assert main(["index", "clear", "--repo", root]) == 0
    assert "cleared" in capsys.readouterr().out


# --- Korean corpus: cache parity + bigram determinism (issue #56, ADR-0012 addendum) -------------

KO_INDEX_MD = "# personal\n\n- [기술 MOC](wiki/maps/ai-tech.md)\n"
KO_MOC = (
    "---\nstatus: active\nkind: map\nsubjects: [ai-tech]\n---\n# 에이전트 기술\n\n"
    "- [큐레이터 동시성](../concepts/curator-concurrency.md) — 단일 작성자 큐레이터가 쓰기를 "
    "직렬화한다\n"
    "- [Memory Hub](../concepts/memory-hub.md) — 크로스 세션 지식 공유\n"
)
KO_CURATOR = (
    "---\nstatus: active\ntags: [concurrency]\n"
    "summary: 큐레이터가 저장소 잠금으로 쓰기를 직렬화한다\n---\n# 큐레이터 동시성\n\n"
    "큐레이터는 저장소 잠금을 획득하여 정확히 하나의 작성자만 브랜치를 전진시킨다.\n"
)
KO_MEMORY_HUB = (
    "---\nstatus: active\naliases: [메모리 허브]\ntags: [memory]\n---\n# Memory Hub\n\n"
    "Agents share one memory hub for cross-session knowledge.\n"
)

# Hangul probes, a mixed-script probe, an alias-only probe, and a mandatory Korean not_found.
KO_QUERIES = ["큐레이터 동시성", "큐레이터가", "메모리 허브", "AI에이전트", "양자 생물학 광합성"]


def _write_ko_corpus(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text(KO_INDEX_MD, encoding="utf-8")
    concepts = root / "wiki" / "concepts"
    concepts.mkdir(parents=True, exist_ok=True)
    (root / "wiki" / "maps").mkdir(parents=True, exist_ok=True)
    (root / "wiki" / "maps" / "ai-tech.md").write_text(KO_MOC, encoding="utf-8")
    (concepts / "curator-concurrency.md").write_text(KO_CURATOR, encoding="utf-8")
    (concepts / "memory-hub.md").write_text(KO_MEMORY_HUB, encoding="utf-8")


def _ko_repo(tmp_path: Path) -> Repo:
    root = tmp_path / "personal"
    _write_ko_corpus(root)
    (root / ".gitignore").write_text("_kb/\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    return Repo.resolve(root)


def test_korean_cached_query_byte_identical_to_uncached(tmp_path: Path) -> None:
    # The #56 parity requirement: CJK-bigram + aliases/summary field_tokens round-trip the cache
    # (JSON with ensure_ascii=False) with byte-identical query output vs the uncached scan.
    repo = _ko_repo(tmp_path)
    layout = repo.layout
    baseline = {q: Wiki(layout).query(q) for q in KO_QUERIES}
    assert baseline["큐레이터 동시성"].status == "ok"
    assert baseline["양자 생물학 광합성"].status == "not_found"
    # the alias-only note must actually be returned (else alias parity below is vacuous)
    assert any(h.path.endswith("memory-hub.md") for h in baseline["메모리 허브"].hits)

    build_cache(repo)
    cached = {q: Wiki(layout).query(q) for q in KO_QUERIES}
    for q in KO_QUERIES:
        assert cached[q] == baseline[q], f"cache changed query output for {q!r}"


def test_korean_double_index_is_byte_identical(tmp_path: Path) -> None:
    # Bigram determinism (#56 test (e)): indexing the SAME Korean corpus twice produces
    # byte-identical cache files — the tokenizer is a pure function of the note bytes.
    repo = _ko_repo(tmp_path)
    build_cache(repo)
    first = repo.layout.index_notes_path().read_bytes()
    build_cache(repo)
    assert repo.layout.index_notes_path().read_bytes() == first


def test_v1_cache_is_invalidated_by_version_bump(tmp_path: Path) -> None:
    # A cache written at CACHE_SCHEMA_VERSION=1 (pre-#56: no CJK bigrams, four-field field_tokens)
    # must be rejected WHOLE by the version gate — its derived tokens are stale even where every
    # source_digest still matches — and the read path must fall back to a correct full scan.
    repo = _ko_repo(tmp_path)
    baseline = Wiki(repo.layout).query("큐레이터 동시성")  # uncached oracle
    assert baseline.status == "ok"
    build_cache(repo)
    path = repo.layout.index_notes_path()
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["cache_schema_version"] = 1
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    assert index_cache.read_payload(path) is None  # the version gate rejects the v1 cache
    assert Wiki(repo.layout).query("큐레이터 동시성") == baseline  # scan fallback, correct output


def test_v2_cache_carrying_a_pre_stratum_map_verdict_is_invalidated(tmp_path: Path) -> None:
    """The ADR-0041 D5 bump, pinned as the stale value it exists to reject (CACHE_SCHEMA_VERSION 3).

    ``is_moc`` is PARSER-COMPUTED and serialized, so a v2 entry whose ``source_digest`` still
    matches would keep a pre-flip map verdict forever — and the entry predates ``kind``/``subjects``
    entirely. Both bump triggers in this module's own contract fire, and the fresh-repo argument
    does not cover it: ``SUPPORTED_KB_SCHEMA_VERSIONS`` keeps 1, so a repo with a populated
    ``_kb/index/`` written by an older build is reachable here.

    The forgery is deliberately the WORST case the gate has to stop — every note flipped to
    ``is_moc: true`` with ``kind``/``subjects`` stripped, i.e. a payload that would both re-seed the
    whole corpus at ``d_moc = 0`` and ``KeyError`` in ``_note_from_dict``. Rejecting it WHOLE at the
    version gate is what keeps the query byte-identical to the scan.
    """
    repo = _repo(tmp_path)
    baseline = _results(repo.layout)  # uncached oracle
    build_cache(repo)
    path = repo.layout.index_notes_path()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["cache_schema_version"] == 3, "the D5 flip must bump the cache schema to 3"
    for entry in doc["notes"].values():
        assert "kind" in entry["note"] and "subjects" in entry["note"]  # the new serialized shape
        entry["note"]["is_moc"] = True  # the stale pre-flip verdict the bump exists to discard
        del entry["note"]["kind"]
        del entry["note"]["subjects"]
    doc["cache_schema_version"] = 2
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    assert index_cache.read_payload(path) is None  # rejected WHOLE, before any entry is read
    for q in QUERIES:
        assert Wiki(repo.layout).query(q) == baseline[q], f"stale v2 cache leaked into {q!r}"
