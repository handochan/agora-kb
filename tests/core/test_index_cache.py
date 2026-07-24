"""Tests for the ADR-0012 §2/§9 derived reader cache (issue #26).

The load-bearing contract is BYTE-IDENTICAL query output whether the cache is absent or present. The
pure-Python scan stays the oracle; the cache only skips re-parsing unchanged files and prunes the
scoring loop via the exact in-memory inverted index (the ADR-0012 §9 FTS5/ripgrep candidate
accelerators are deferred to a load-avoiding reader — issue #28).

The fixture is a git-initialized repo (``branch_commit()`` must resolve to key the cache) carrying
the ADR-0012 §10 corpus PLUS a basename-derived-title note and an abutting-links note whose only
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

INDEX_MD = "# personal\n\n- [AI Tech MOC](wiki/ai-tech/ai-tech-moc.md)\n"
MOC = (
    "---\nstatus: active\n---\n# AI Tech\n\n"
    "- [Curator concurrency](themes/curator-concurrency.md) — single-writer curator serializes\n"
    "- [Inbox design](themes/inbox-design.md) — append-only per-writer inbox\n"
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
    themes = root / "wiki" / "ai-tech" / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    (root / "wiki" / "ai-tech" / "ai-tech-moc.md").write_text(MOC, encoding="utf-8")
    (themes / "curator-concurrency.md").write_text(CURATOR_CONCURRENCY, encoding="utf-8")
    (themes / "inbox-design.md").write_text(INBOX_DESIGN, encoding="utf-8")
    (themes / "zephyrquux-topic.md").write_text(BASENAME_TITLE_NOTE, encoding="utf-8")
    (themes / "synth-token.md").write_text(SYNTH_TOKEN_NOTE, encoding="utf-8")


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
    new = repo.root / "wiki" / "ai-tech" / "themes" / "raftlog-note.md"
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
    note = repo.root / "wiki" / "ai-tech" / "themes" / "inbox-design.md"
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
    target = "wiki/ai-tech/themes/curator-concurrency.md"  # the note the query hits
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
    with pytest.raises(InvalidWriterError):
        layout.index_notes_path("../escape")


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


# --- Korean corpus: cache parity + bigram determinism (issue #56, ADR-0012 addendum) -------------

KO_INDEX_MD = "# personal\n\n- [기술 MOC](wiki/ai-tech/ai-tech-moc.md)\n"
KO_MOC = (
    "---\nstatus: active\n---\n# 에이전트 기술\n\n"
    "- [큐레이터 동시성](themes/curator-concurrency.md) — 단일 작성자 큐레이터가 쓰기를 "
    "직렬화한다\n"
    "- [Memory Hub](themes/memory-hub.md) — 크로스 세션 지식 공유\n"
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
    themes = root / "wiki" / "ai-tech" / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    (root / "wiki" / "ai-tech" / "ai-tech-moc.md").write_text(KO_MOC, encoding="utf-8")
    (themes / "curator-concurrency.md").write_text(KO_CURATOR, encoding="utf-8")
    (themes / "memory-hub.md").write_text(KO_MEMORY_HUB, encoding="utf-8")


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
