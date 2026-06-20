"""Tests for FileConnector link-following (agora_kb.harvester.connectors; ADR-0018).

Covers the link-extraction allowlist/sanitizer, the tolerant frontmatter strip, fact composition
(H1 reuse + gloss preservation + body-only fact_key), and the scan behaviors: compose-not-replace,
never-drop fallback, the path-safety guarantees (source-dir-subtree containment, symlink-target
reject, self-reference skip, fan-out cap), one-level-only (no recursion), and the D7 no-op hash that
folds sibling bytes so a sibling-only edit is re-harvested.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.harvester.connectors import (
    FileConnector,
    Scope,
    _clean_link_path,
    _compose_followed_fact,
    _extract_local_md_links,
    _strip_frontmatter,
)

# --- _clean_link_path -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("slug.md", "slug.md"),
        ("./sub/note.md", "./sub/note.md"),
        ('slug.md "a title"', "slug.md"),  # drop a title
        ("slug.md#section", "slug.md"),  # strip fragment
        ("slug.md?x=1", "slug.md"),  # strip query
        ("<my note.md>", "my note.md"),  # angle-bracket form keeps spaces
        ("SLUG.MD", "SLUG.MD"),  # case-insensitive .md suffix
    ],
)
def test_clean_link_path_accepts(raw: str, expected: str) -> None:
    assert _clean_link_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "https://example.com/x.md",  # URL scheme
        "mailto:x@y.md",  # scheme
        "/etc/passwd.md",  # absolute
        "~/secret.md",  # home-rooted
        "a%2e%2e/b.md",  # percent-encoded (traversal kept impossible)
        "a\\b.md",  # backslash
        ".env.md",  # dotfile final component
        "sub/.secret.md",  # dotfile final component in a subdir
        "notes.txt",  # not .md
        "slug.md.txt",  # not .md after the real suffix
    ],
)
def test_clean_link_path_rejects(raw: str) -> None:
    assert _clean_link_path(raw) is None


def test_extract_links_source_order_dedup_and_image_exclusion() -> None:
    block = "- see [A](a.md) and [B](b.md) and again [A2](a.md) and image ![x](c.md)"
    assert _extract_local_md_links(block) == [("A", "a.md"), ("B", "b.md")]


# --- _strip_frontmatter ---------------------------------------------------------------------------


def test_strip_frontmatter_removes_leading_fence() -> None:
    text = "---\nname: x\ndesc: y\n---\n# Title\n\nbody here\n"
    assert _strip_frontmatter(text) == "# Title\n\nbody here"


def test_strip_frontmatter_no_fence_is_kept() -> None:
    text = "# Title\n\njust a body\n"
    assert _strip_frontmatter(text) == "# Title\n\njust a body"


def test_strip_frontmatter_tolerates_malformed_yaml() -> None:
    # An Obsidian-style malformed YAML fence must NOT raise (we never call frontmatter.parse).
    text = "---\nlinks: [[a]], [[b]]\n---\nthe body\n"
    assert _strip_frontmatter(text) == "the body"


def test_strip_frontmatter_unterminated_fence_keeps_whole() -> None:
    text = "---\nname: x\nno closing fence\nbody\n"
    assert _strip_frontmatter(text) == text.strip("\n")


# --- _compose_followed_fact -----------------------------------------------------------------------


def test_compose_reuses_sibling_h1_and_preserves_gloss() -> None:
    composed, body = _compose_followed_fact(
        "Link Text", "— my note", "---\na: b\n---\n# Real H1\n\nprose\n"
    )
    assert composed == "> — my note\n\n# Real H1\n\nprose"
    assert body == "# Real H1\n\nprose"  # body-only, frontmatter stripped (used for fact_key)


def test_compose_synthesizes_title_when_no_h1() -> None:
    composed, body = _compose_followed_fact("My Title", "", "just prose, no heading\n")
    assert composed == "# My Title\n\njust prose, no heading"
    assert body == "just prose, no heading"


# --- scan: follow_links -------------------------------------------------------------------------


def _conn(path: Path, *, follow: bool = True, **kw: object) -> FileConnector:
    return FileConnector(
        name="file:x", path=str(path), scope=Scope.personal, follow_links=follow, **kw
    )


def test_follow_emits_sibling_content(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text(
        "# Index\n\n- [Curator](curator.md) — how it works\n", encoding="utf-8"
    )
    (tmp_path / "curator.md").write_text(
        "---\nname: curator\n---\n# Curator\n\nOne curator holds a per-repo lock.\n",
        encoding="utf-8",
    )
    scan = _conn(tmp_path / "MEMORY.md").scan(last_content_sha256=None)
    assert len(scan.facts) == 1
    text = scan.facts[0].text
    assert "One curator holds a per-repo lock." in text
    assert "> — how it works" in text  # gloss preserved
    assert "name: curator" not in text  # frontmatter stripped


def test_follow_links_false_is_thin_pointer(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("# I\n\n- [C](curator.md) — note\n", encoding="utf-8")
    (tmp_path / "curator.md").write_text("# Curator\n\nbody\n", encoding="utf-8")
    scan = _conn(tmp_path / "MEMORY.md", follow=False).scan(last_content_sha256=None)
    assert [f.text for f in scan.facts] == ["- [C](curator.md) — note"]


def test_broken_link_falls_back_to_bullet(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("# I\n\n- [Gone](missing.md) — note\n", encoding="utf-8")
    scan = _conn(tmp_path / "MEMORY.md").scan(last_content_sha256=None)
    assert [f.text for f in scan.facts] == ["- [Gone](missing.md) — note"]
    assert any("not followed" in n for n in scan.notes)


def test_escape_source_dir_blocked(tmp_path: Path) -> None:
    # Source-dir containment: projA may NOT read projB even though both are under the glob root.
    (tmp_path / "projA").mkdir()
    (tmp_path / "projB").mkdir()
    (tmp_path / "projA" / "MEMORY.md").write_text(
        "# I\n\n- [Secret](../projB/secret.md) — x\n", encoding="utf-8"
    )
    (tmp_path / "projB" / "secret.md").write_text("# Secret\n\nprivate\n", encoding="utf-8")
    scan = _conn(tmp_path / "**" / "MEMORY.md").scan(last_content_sha256=None)
    joined = " ".join(f.text for f in scan.facts)
    assert "private" not in joined  # the cross-dir read was refused
    assert any("escapes source dir" in n for n in scan.notes)


def test_symlinked_target_rejected(tmp_path: Path) -> None:
    (tmp_path / "real.md").write_text("# Real\n\nsecret-ish\n", encoding="utf-8")
    (tmp_path / "link.md").symlink_to(tmp_path / "real.md")
    (tmp_path / "MEMORY.md").write_text("# I\n\n- [L](link.md) — x\n", encoding="utf-8")
    scan = _conn(tmp_path / "MEMORY.md").scan(last_content_sha256=None)
    assert "secret-ish" not in " ".join(f.text for f in scan.facts)
    assert any("symlink target rejected" in n for n in scan.notes)


def test_self_reference_to_index_skipped(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("# I\n\n- [self](MEMORY.md) — x\n", encoding="utf-8")
    scan = _conn(tmp_path / "MEMORY.md").scan(last_content_sha256=None)
    assert any("self-reference" in n for n in scan.notes)
    # falls back to the bullet (the index is not harvested as one giant fact)
    assert scan.facts[0].text == "- [self](MEMORY.md) — x"


def test_non_md_link_not_followed(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("# I\n\n- [cfg](config.toml) — x\n", encoding="utf-8")
    (tmp_path / "config.toml").write_text("secret = 1\n", encoding="utf-8")
    scan = _conn(tmp_path / "MEMORY.md").scan(last_content_sha256=None)
    # A non-.md link is never extracted as followable → the bullet is kept verbatim.
    assert scan.facts[0].text == "- [cfg](config.toml) — x"


def test_oversized_sibling_falls_back(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("# I\n\n- [Big](big.md) — x\n", encoding="utf-8")
    (tmp_path / "big.md").write_text("# Big\n\n" + "z" * 5000, encoding="utf-8")
    scan = _conn(tmp_path / "MEMORY.md", max_file_bytes=128).scan(last_content_sha256=None)
    assert scan.facts[0].text == "- [Big](big.md) — x"
    assert any("exceeds max_file_bytes" in n for n in scan.notes)


def test_fan_out_cap(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"s{i}.md").write_text(f"# S{i}\n\nbody {i}\n", encoding="utf-8")
    bullets = "".join(f"- [S{i}](s{i}.md)\n" for i in range(5))
    (tmp_path / "MEMORY.md").write_text(f"# I\n\n{bullets}", encoding="utf-8")
    scan = _conn(tmp_path / "MEMORY.md", max_followed=2).scan(last_content_sha256=None)
    followed = [f for f in scan.facts if "body" in f.text]
    assert len(followed) == 2
    assert any("max_followed" in n for n in scan.notes)


def test_one_level_only_no_recursion(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("# I\n\n- [B](b.md) — x\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\n\nB body. See [C](c.md)\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("# C\n\nC-UNIQUE-CONTENT\n", encoding="utf-8")
    scan = _conn(tmp_path / "MEMORY.md").scan(last_content_sha256=None)
    joined = " ".join(f.text for f in scan.facts)
    assert "B body" in joined
    assert "C-UNIQUE-CONTENT" not in joined  # the sibling's own link is NOT followed


def test_sibling_only_edit_reharvests_when_following(tmp_path: Path) -> None:
    # D7: a sibling edit with a byte-identical index must NOT be a no-op (siblings in the hash).
    (tmp_path / "MEMORY.md").write_text("# I\n\n- [S](s.md) — x\n", encoding="utf-8")
    s = tmp_path / "s.md"
    s.write_text("# S\n\nversion one\n", encoding="utf-8")
    conn = _conn(tmp_path / "MEMORY.md")
    first = conn.scan(last_content_sha256=None)
    s.write_text("# S\n\nversion two\n", encoding="utf-8")  # edit ONLY the sibling
    second = conn.scan(last_content_sha256=first.content_sha256)
    assert second.unchanged is False
    assert "version two" in " ".join(f.text for f in second.facts)


def test_same_sibling_two_links_dedupes_on_body(tmp_path: Path) -> None:
    # Two different bullets pointing at the same sibling → identical fact_key (body-only) → dedup.
    (tmp_path / "MEMORY.md").write_text(
        "# I\n\n- [One](s.md) — a\n- [Two](s.md) — b\n", encoding="utf-8"
    )
    (tmp_path / "s.md").write_text("# S\n\nshared body\n", encoding="utf-8")
    scan = _conn(tmp_path / "MEMORY.md").scan(last_content_sha256=None)
    keys = {f.fact_key for f in scan.facts}
    assert len(keys) == 1  # both collapse to one dedup identity


def test_traversal_attack_blocked(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text(
        "# I\n\n- [pwn](../../../../etc/passwd.md) — x\n", encoding="utf-8"
    )
    scan = _conn(tmp_path / "MEMORY.md").scan(last_content_sha256=None)
    # Falls back to the bullet; nothing outside the source dir is read.
    assert scan.facts[0].text == "- [pwn](../../../../etc/passwd.md) — x"
    assert any("not followed" in n for n in scan.notes)


# --- review-finding regressions -----------------------------------------------------------------


def test_mixed_block_keeps_unfollowed_pointer(tmp_path: Path) -> None:
    # A block with one resolvable + one dead link: the sibling is harvested AND the original bullet
    # is kept verbatim, so the dead pointer is never silently dropped (review finding #1 / D7).
    (tmp_path / "good.md").write_text("# Good\n\ngood body\n", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text(
        "# I\n\n- [Good](good.md) and [Bad](missing.md) — note\n", encoding="utf-8"
    )
    texts = [f.text for f in _conn(tmp_path / "MEMORY.md").scan(last_content_sha256=None).facts]
    assert any("good body" in t for t in texts)  # the resolvable link was followed
    assert any("missing.md" in t for t in texts)  # the dead pointer survives in the kept bullet


def test_max_followed_counts_broken_attempts(tmp_path: Path) -> None:
    # The fan-out cap counts ATTEMPTS, so an index of broken links is bounded (review finding #3).
    bullets = "".join(f"- [B{i}](missing{i}.md)\n" for i in range(5))
    (tmp_path / "MEMORY.md").write_text(f"# I\n\n{bullets}", encoding="utf-8")
    scan = _conn(tmp_path / "MEMORY.md", max_followed=2).scan(last_content_sha256=None)
    assert any("max_followed" in n for n in scan.notes)


def test_empty_body_sibling_kept_as_pointer(tmp_path: Path) -> None:
    # A frontmatter-only sibling carries no knowledge → keep the pointer, don't emit a bodyless fact
    # whose key would not dedup (review finding #5 / D5).
    (tmp_path / "empty.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("# I\n\n- [E](empty.md) — note\n", encoding="utf-8")
    scan = _conn(tmp_path / "MEMORY.md").scan(last_content_sha256=None)
    assert any("sibling body is empty" in n for n in scan.notes)
    assert scan.facts[0].text == "- [E](empty.md) — note"


def test_max_facts_midfollow_keeps_noop_hash_complete(tmp_path: Path) -> None:
    # Hitting max_facts must NOT corrupt the D7 hash: a sibling edit beyond the fact cutoff is still
    # detected because every followed sibling is folded into the hash (review finding #2).
    for i in range(4):
        (tmp_path / f"s{i}.md").write_text(f"# S{i}\n\nbody {i}\n", encoding="utf-8")
    bullets = "".join(f"- [S{i}](s{i}.md)\n" for i in range(4))
    (tmp_path / "MEMORY.md").write_text(f"# I\n\n{bullets}", encoding="utf-8")
    conn = _conn(tmp_path / "MEMORY.md", max_facts=2)
    first = conn.scan(last_content_sha256=None)
    assert any("max_facts" in n for n in first.notes)
    (tmp_path / "s3.md").write_text(
        "# S3\n\nEDITED body 3\n", encoding="utf-8"
    )  # beyond the cutoff
    second = conn.scan(last_content_sha256=first.content_sha256)
    assert second.unchanged is False  # the beyond-cutoff sibling edit was detected


def test_follow_off_vs_on_byte_identical_when_no_links(tmp_path: Path) -> None:
    # ADR-0018 D1: follow_links is a strict superset — with NO links, off and on are byte-identical
    # (same facts, same whole-source hash → cursor portable across the toggle) (review finding #8).
    (tmp_path / "MEMORY.md").write_text(
        "# I\n\n- plain fact one\n- plain fact two\n", encoding="utf-8"
    )
    off = _conn(tmp_path / "MEMORY.md", follow=False).scan(last_content_sha256=None)
    on = _conn(tmp_path / "MEMORY.md", follow=True).scan(last_content_sha256=None)
    assert off.content_sha256 == on.content_sha256
    assert [f.text for f in off.facts] == [f.text for f in on.facts]


def test_setext_h1_sibling_not_double_titled() -> None:
    composed, _ = _compose_followed_fact("Link Text", "", "Real Title\n==========\n\nprose\n")
    assert composed == "Real Title\n==========\n\nprose"  # sibling's own setext title reused


def test_h2_leading_sibling_gets_synthesized_title() -> None:
    composed, _ = _compose_followed_fact("My Title", "", "## Section\n\nprose\n")
    assert composed == "# My Title\n\n## Section\n\nprose"


def test_crlf_sibling_is_normalized(tmp_path: Path) -> None:
    (tmp_path / "s.md").write_bytes(b"# S\r\n\r\nbody line\r\n")
    (tmp_path / "MEMORY.md").write_text("# I\n\n- [S](s.md)\n", encoding="utf-8")
    scan = _conn(tmp_path / "MEMORY.md").scan(last_content_sha256=None)
    assert "\r" not in scan.facts[0].text
    assert "body line" in scan.facts[0].text
