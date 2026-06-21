"""D4 tolerant-consumer read boundary (ADR-0014 D4): the read/lint path must never crash.

Agora is a STRICT producer but a TOLERANT consumer (ADR-0014 D1): the read path (``kb_query`` /
``core.Wiki``), the deterministic ``schema.lint``, and ``schema.notes.parse_all_notes`` MUST NOT
raise an uncaught exception on foreign or imperfect content — a not-yet-normalized Obsidian vault,
a harvested bundle, or a partially-migrated repo. They surface a finding or skip; they never crash.

These lock in that contract against an adversarial vault: a non-UTF8 note, malformed-YAML Obsidian
frontmatter, unknown frontmatter keys, broken/foreign links (wikilink + markdown + image embed), a
note with no closing fence, and a ``.canvas`` / ``.obsidian`` sidecar. (The STRICT UTF-8 / link /
frontmatter rules remain the producer lint's job; here we only assert the READER tolerates them.)
"""

from __future__ import annotations

from pathlib import Path

from agora_kb.core import Wiki
from agora_kb.core.layout import RepoLayout
from agora_kb.schema.lint import lint
from agora_kb.schema.notes import parse_all_notes


def _adversarial_vault(tmp_path: Path) -> Path:
    """Build a vault full of the real-world imperfections a tolerant consumer must survive."""
    root = tmp_path / "vault"
    (root / "wiki" / "general" / "themes").mkdir(parents=True)
    # malformed-YAML Obsidian inline-wikilink frontmatter (invalid YAML) + a body wikilink
    (root / "index.md").write_text(
        "---\ntitle: I\nlinks: [[a]], [[b]]\n---\n# Index\n- [[a]]\n", encoding="utf-8"
    )
    # unknown frontmatter keys + a broken wikilink + a broken markdown link + an image embed
    (root / "wiki" / "general" / "themes" / "a.md").write_text(
        "---\ntitle: A\ncssclass: foo\n---\nbody [[ghost]] [x](nowhere.md) ![[diagram.png]]\n",
        encoding="utf-8",
    )
    # NON-UTF8 bytes (latin-1 é + stray 0xff/0xfe) — the crash this test was written for
    (root / "wiki" / "general" / "themes" / "b.md").write_bytes(
        b"---\ntitle: B\n---\ncaf\xe9 r\xe9sum\xe9 \xff\xfe non-utf8 marker token\n"
    )
    # frontmatter with no closing fence
    (root / "wiki" / "general" / "themes" / "c.md").write_text(
        "---\ntitle: C\nno closing fence here\n", encoding="utf-8"
    )
    # Obsidian sidecars that are not notes
    (root / "wiki" / "general" / "diagram.canvas").write_text('{"nodes":[]}', encoding="utf-8")
    (root / ".obsidian").mkdir()
    (root / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    return root


def test_query_does_not_crash_on_adversarial_vault(tmp_path: Path) -> None:
    """``kb_query`` tolerates every imperfection — it returns a result, never raises."""
    root = _adversarial_vault(tmp_path)
    result = Wiki(RepoLayout(root)).query("anything at all")
    assert result.status in ("ok", "not_found")


def test_query_still_finds_a_non_utf8_note(tmp_path: Path) -> None:
    """A non-UTF8 note is decoded LOSSILY and stays queryable (no UnicodeDecodeError out of query).

    This is the exact crash this whole D4 lock-in was written to prevent.
    """
    root = _adversarial_vault(tmp_path)
    result = Wiki(RepoLayout(root)).query("non-utf8 marker token")
    assert result.status == "ok"
    assert any("b.md" in hit.path for hit in result.hits)


def test_lint_does_not_crash_on_adversarial_vault(tmp_path: Path) -> None:
    """The deterministic lint surfaces FINDINGS on foreign content rather than raising."""
    root = _adversarial_vault(tmp_path)
    result = lint(RepoLayout(root))
    assert result.ok is False  # there ARE problems
    assert result.findings  # ...reported as findings, not a crash


def test_parse_all_notes_does_not_crash_on_adversarial_vault(tmp_path: Path) -> None:
    """``parse_all_notes`` is a TOLERANT consumer by default and raises only the typed error strict.

    Default (the browse/read substrate): NEVER raises — a fenceless / malformed note degrades to
    empty frontmatter + full body, so the read path stays up (ADR-0014 D1). ``strict=True`` (the
    producer lint / curator grading substrate): raises only the typed ``FrontmatterError`` its
    callers already handle — never an uncaught ``UnicodeDecodeError`` / ``yaml.YAMLError`` leaking
    out of the consumer path.
    """
    from agora_kb.core.frontmatter import FrontmatterError

    root = _adversarial_vault(tmp_path)
    # Tolerant default: every note (incl. the no-closing-fence c.md) parses without raising.
    notes = parse_all_notes(RepoLayout(root))
    assert any(n.rel_path.endswith("c.md") for n in notes)
    # strict=True: fail-fast, but only the typed, caller-handled FrontmatterError.
    try:
        parse_all_notes(RepoLayout(root), strict=True)
    except FrontmatterError:
        pass  # acceptable: the typed, caller-handled error


def test_lint_and_parse_tolerate_a_sole_non_utf8_note(tmp_path: Path) -> None:
    """A non-UTF8 note that is the ONLY issue (no earlier malformed note to short-circuit on) must
    NOT crash parse_all_notes / lint with UnicodeDecodeError — it is decoded lossily and the
    byte-level L1-16 encoding rule flags it (ADR-0014 D4)."""
    root = tmp_path / "v"
    (root / "wiki" / "general" / "themes").mkdir(parents=True)
    (root / "wiki" / "general" / "themes" / "b.md").write_bytes(
        b"---\ntitle: B\ntype: theme\nstatus: active\n"
        b"created: 2026-06-18\nupdated: 2026-06-18\nsummary: s\n---\ncaf\xe9 \xff body\n"
    )
    notes = parse_all_notes(RepoLayout(root))  # must not raise UnicodeDecodeError
    assert len(notes) == 1
    result = lint(RepoLayout(root))  # must not raise; flags the bad encoding
    assert any(f.code == "L1-16" for f in result.findings)
