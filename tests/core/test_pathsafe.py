"""Tests for the Unicode-safe path-component allowlist (:mod:`agora_kb.core.pathsafe`).

The module has NO call sites yet (Stratum UNIT 1) — these tests are the whole proof. They are what
makes a later regression attributable: if swapping ``plan.py`` / ``ollama_brain.py`` onto this
function breaks something, this corpus stays green and the failure is the swap, not the charset.
"""

from __future__ import annotations

import re
import unicodedata

import pytest

from agora_kb.core.pathsafe import (
    DEFAULT_MAX_BYTES,
    is_safe_component,
    safe_slug_component,
)

# The PATH/ALLOWLIST safe-token regex the curator uses TODAY (``curator/plan.py``'s
# ``_SAFE_TOKEN_RE_PATTERN``), copied (not imported) so this file keeps proving the relationship
# even if plan.py later moves onto pathsafe.
_TODAY_ASCII_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


# --- the hostile corpus ---------------------------------------------------------------------
#
# (input, expected) pairs. ``""`` means "nothing safe survives" — the signal that keeps the #57
# ``note-<sha8>`` floor alive at the (future) call site.
HOSTILE_CASES: list[tuple[str, str]] = [
    # -- traversal & separators ----------------------------------------------------------------
    ("..", ""),  # leading-dot reject is what kills the classic traversal token
    (".", ""),
    ("...", ""),
    ("../etc/passwd", ""),  # collapses to "..-etc-passwd", then dies on the leading dot
    ("a/b", "a-b"),
    ("a\\b", "a-b"),
    ("a//b", "a-b"),  # a separator RUN collapses to one "-"
    ("/a/", "a"),
    ("a/../b", "a-..-b"),  # ".." INSIDE one component is a legal filename, not traversal
    # -- NUL and control characters -------------------------------------------------------------
    ("a\x00b", "a-b"),
    ("\x00", ""),
    ("a\x01b", "a-b"),
    ("a\x07b", "a-b"),
    ("a\x1fb", "a-b"),
    ("\x01\x02\x1f", ""),
    ("a\tb", "a-b"),
    ("a\nb", "a-b"),
    ("a\r\nb", "a-b"),  # CR+LF is ONE separator run
    ("a\x7fb", "a-b"),  # DEL
    ("a\x85b", "a-b"),  # C1 NEL
    # -- bidi / invisible / lookalike separators ------------------------------------------------
    ("a‮b", "a-b"),  # RIGHT-TO-LEFT OVERRIDE (filename spoofing)
    ("‮", ""),
    ("a​b", "a-b"),  # ZERO WIDTH SPACE
    ("a﻿b", "a-b"),  # ZERO WIDTH NO-BREAK SPACE / BOM
    ("a‎b", "a-b"),  # LEFT-TO-RIGHT MARK
    ("a／b", "a-b"),  # FULLWIDTH SOLIDUS — NOT a path separator, but reads as one
    ("　", ""),  # IDEOGRAPHIC SPACE
    ("가　나", "가-나"),
    # -- the Windows-hostile punctuation set ----------------------------------------------------
    ('<>:"|?*', ""),
    ("a<b>c", "a-b-c"),
    ("a:b", "a-b"),
    ('a"b', "a-b"),
    ("a|b", "a-b"),
    ("a?b", "a-b"),
    ("a*b", "a-b"),
    # -- Windows reserved device stems (today's ASCII regex ADMITS every one of these) ----------
    ("CON", ""),
    ("con", ""),
    ("Con", ""),
    ("con.md", ""),
    ("CON.MD", ""),
    ("PRN", ""),
    ("aux", ""),
    ("aux.md", ""),
    ("NUL.txt", ""),
    ("COM1", ""),
    ("com9.md", ""),
    ("LPT1", ""),
    ("lpt9.txt", ""),
    ("com0", ""),  # reserved — the device set is COM0-9, not just COM1-9
    ("lpt0", ""),
    ("com¹", ""),  # superscript device forms — the Win32 parser resolves these too
    ("COM²", ""),
    ("lpt³", ""),
    ("com10", "com10"),
    ("console", "console"),  # a reserved stem is the WHOLE stem, not a prefix
    ("conference-notes", "conference-notes"),
    # -- trailing space / dot (Windows strips these silently) -----------------------------------
    ("x ", "x"),
    ("x.", "x"),
    ("x...", "x"),
    ("x. . .", "x"),
    (" x", "x"),
    ("  x  ", "x"),
    ("-leading", "leading"),
    ("trailing-", "trailing"),
    ("---", ""),
    ("___", "___"),  # "_" is admitted literally and is not an edge character
    # -- dotfiles -------------------------------------------------------------------------------
    (".hidden", ""),
    ("...hidden", ""),
    ("-.hidden", ""),  # the leading "-" strips first, EXPOSING the dot — still rejected
    ("a.hidden", "a.hidden"),
    # -- shell / URL metacharacters -------------------------------------------------------------
    ("~/secret", "secret"),
    ("$HOME", "HOME"),
    ("a%2fb", "a-2fb"),
    ("a&b;c", "a-b-c"),
    ("`cmd`", "cmd"),
    # -- non-ASCII that MUST survive (the whole point of the widening) --------------------------
    ("한국어", "한국어"),
    ("한국어 메모", "한국어-메모"),
    ("메모/노트", "메모-노트"),
    ("日本語", "日本語"),
    ("Привет", "Привет"),
    ("café", "café"),
    ("emoji😀here", "emoji-here"),  # So is a separator; the letters survive
    ("😀", ""),
    # -- plain ASCII the current plan.py regex already accepts ----------------------------------
    ("hello-world", "hello-world"),
    ("my.note_v2-1", "my.note_v2-1"),
    ("Note1", "Note1"),
    ("123", "123"),
    ("a", "a"),
    # -- empty / all-punctuation ----------------------------------------------------------------
    ("", ""),
    ("!!!", ""),
    ("   ", ""),
    ("+++", ""),
]


@pytest.mark.parametrize(("raw", "expected"), HOSTILE_CASES, ids=lambda v: repr(v)[:40])
def test_hostile_corpus(raw: str, expected: str) -> None:
    assert safe_slug_component(raw) == expected


def test_corpus_is_large_enough() -> None:
    # The unit spec asks for >= 60 hostile cases; this asserts the corpus is not silently gutted.
    assert len(HOSTILE_CASES) >= 60


# --- properties -----------------------------------------------------------------------------

_PROPERTY_INPUTS: list[str] = [raw for raw, _ in HOSTILE_CASES] + [
    "가" * 200,
    "a" * 500,
    "한국어-메모-2026",
    "́combining-mark-first",
    "à",
    "-" * 300,
    "." * 300,
    "\x00" * 50,
    "Ω-Ω",
]


@pytest.mark.parametrize("raw", _PROPERTY_INPUTS, ids=lambda v: repr(v)[:40])
def test_idempotent(raw: str) -> None:
    once = safe_slug_component(raw)
    assert safe_slug_component(once) == once


@pytest.mark.parametrize("raw", _PROPERTY_INPUTS, ids=lambda v: repr(v)[:40])
def test_output_never_contains_a_separator_or_nul(raw: str) -> None:
    out = safe_slug_component(raw)
    assert "/" not in out
    assert "\\" not in out
    assert "\x00" not in out


@pytest.mark.parametrize("raw", _PROPERTY_INPUTS, ids=lambda v: repr(v)[:40])
def test_output_never_starts_with_a_dot(raw: str) -> None:
    assert not safe_slug_component(raw).startswith(".")


@pytest.mark.parametrize("raw", _PROPERTY_INPUTS, ids=lambda v: repr(v)[:40])
def test_output_respects_the_byte_cap(raw: str) -> None:
    assert len(safe_slug_component(raw).encode("utf-8")) <= DEFAULT_MAX_BYTES


@pytest.mark.parametrize("raw", _PROPERTY_INPUTS, ids=lambda v: repr(v)[:40])
def test_output_has_no_edge_or_trailing_windows_characters(raw: str) -> None:
    out = safe_slug_component(raw)
    if out:
        assert not out.startswith("-")
        assert not out.endswith(("-", ".", " "))


@pytest.mark.parametrize("raw", _PROPERTY_INPUTS, ids=lambda v: repr(v)[:40])
def test_output_is_nfc_stable(raw: str) -> None:
    # Dropping codepoints out of an NFC string can in principle re-expose a composable pair; the
    # implementation replaces (rather than deletes) interior separators so it cannot. Locked here.
    out = safe_slug_component(raw)
    assert unicodedata.normalize("NFC", out) == out


# --- ASCII compatibility with the gate that exists today --------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["hello-world", "my.note_v2-1", "Note1", "123", "a", "A1_b-c.d", "z9"],
    ids=lambda v: repr(v),
)
def test_ascii_tokens_the_current_regex_accepts_are_unchanged(raw: str) -> None:
    assert _TODAY_ASCII_RE.match(raw), "fixture must be a token TODAY's plan.py regex accepts"
    assert safe_slug_component(raw) == raw


@pytest.mark.parametrize("raw", ["CON", "com1.md", "x.", "abc."], ids=lambda v: repr(v))
def test_deliberate_tightenings_over_the_current_regex(raw: str) -> None:
    # These pass ``plan.py``'s regex today and are DELIBERATELY narrowed here: a Windows reserved
    # device stem, and a trailing dot Windows would silently strip (letting two tokens name one
    # file). Documented as intentional so a future reader does not "fix" it back.
    assert _TODAY_ASCII_RE.match(raw)
    assert safe_slug_component(raw) != raw


# --- Unicode normalization --------------------------------------------------------------------


def test_nfd_and_nfc_korean_produce_the_same_component() -> None:
    nfc = unicodedata.normalize("NFC", "한글 메모")
    nfd = unicodedata.normalize("NFD", "한글 메모")
    assert nfc != nfd, "fixture guard: the two forms must actually differ in codepoints"
    assert safe_slug_component(nfd) == safe_slug_component(nfc) == "한글-메모"


def test_nfd_and_nfc_latin_accents_produce_the_same_component() -> None:
    assert safe_slug_component("café") == safe_slug_component("café") == "café"


def test_combining_mark_is_kept_and_composed() -> None:
    # Category M is in the allowlist; NFC then composes it onto its base where a composition exists.
    assert safe_slug_component("à") == "à"


def test_homographs_do_not_collapse_stated_residual() -> None:
    # Cyrillic "а" (U+0430) and Latin "a" (U+0061) are both Ll, so BOTH survive and produce two
    # visually identical filenames. This is a documented residual of an allowlist-by-category:
    # confusable detection is a different control and is deliberately NOT in this function.
    cyrillic = "аbc"
    latin = "abc"
    assert cyrillic != latin
    assert safe_slug_component(cyrillic) == cyrillic
    assert safe_slug_component(latin) == latin
    assert safe_slug_component(cyrillic) != safe_slug_component(latin)


def test_a_lone_leading_combining_mark_survives_stated_residual() -> None:
    # Category M is in the allowlist, and NFC cannot compose a mark that has no base before it, so
    # a component may BEGIN with a combining mark. It is a legal filename everywhere Agora runs and
    # it cannot escape a directory, so it is pinned as behaviour rather than rejected — but it is
    # pinned deliberately, so a later "reject leading Mn" rule is a visible decision, not a drift.
    raw = "́combining-mark-first"
    assert safe_slug_component(raw) == raw


# --- the UTF-8 byte cap -------------------------------------------------------------------------


def test_long_korean_is_truncated_to_the_byte_cap_without_breaking_a_character() -> None:
    raw = "가" * 67  # 201 UTF-8 bytes — over the default 180-byte cap
    assert len(raw.encode("utf-8")) > DEFAULT_MAX_BYTES
    out = safe_slug_component(raw)
    encoded = out.encode("utf-8")
    assert len(encoded) <= DEFAULT_MAX_BYTES
    assert encoded.decode("utf-8") == out  # no dangling continuation byte
    assert out == "가" * 60  # 180 bytes exactly
    assert raw.startswith(out)


def test_byte_cap_backs_off_when_the_boundary_falls_mid_character() -> None:
    raw = "가" * 50
    out = safe_slug_component(raw, max_bytes=100)  # 100 is NOT a multiple of 3
    assert out == "가" * 33  # 99 bytes; the 34th syllable would need 102
    assert len(out.encode("utf-8")) <= 100


def test_byte_cap_is_bytes_not_characters() -> None:
    # The character slice used by today's ASCII slugger would have kept 180 SYLLABLES (540 bytes).
    out = safe_slug_component("한" * 300)
    assert len(out) == 60
    assert len(out.encode("utf-8")) == 180


def test_truncation_re_strips_an_exposed_trailing_separator() -> None:
    assert safe_slug_component("abc-def", max_bytes=4) == "abc"


def test_truncation_re_checks_the_windows_reserved_stems() -> None:
    # "console" is fine; truncated to 3 bytes it would become the device name "con".
    assert safe_slug_component("console") == "console"
    assert safe_slug_component("console", max_bytes=3) == ""


def test_ascii_is_unaffected_by_the_byte_cap_when_it_fits() -> None:
    raw = "a" * DEFAULT_MAX_BYTES
    assert safe_slug_component(raw) == raw


def test_non_positive_max_bytes_yields_the_rejection_sentinel() -> None:
    assert safe_slug_component("hello", max_bytes=0) == ""
    assert safe_slug_component("hello", max_bytes=-1) == ""


# --- is_safe_component ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw", ["hello-world", "한국어", "my.note_v2-1", "Note1", "___"], ids=lambda v: repr(v)
)
def test_is_safe_component_true_for_canonical_tokens(raw: str) -> None:
    assert is_safe_component(raw)


@pytest.mark.parametrize(
    "raw",
    ["..", "a/b", "CON", "x.", ".hidden", "a b", "\x00", "가" * 67],
    ids=lambda v: repr(v)[:20],
)
def test_is_safe_component_false_for_hostile_tokens(raw: str) -> None:
    assert not is_safe_component(raw)


def test_is_safe_component_rejects_the_empty_string() -> None:
    # "" is safe_slug_component's REJECTION SENTINEL, not a usable path component, so the literal
    # ``f(x) == x`` reading is deliberately guarded — an empty filename is never safe.
    assert safe_slug_component("") == ""
    assert not is_safe_component("")


def test_is_safe_component_agrees_with_the_slugger_on_the_whole_corpus() -> None:
    for raw, _ in HOSTILE_CASES:
        assert is_safe_component(raw) == (raw != "" and safe_slug_component(raw) == raw)


# --- a real filesystem round trip ----------------------------------------------------------------


def test_nfc_component_round_trips_through_a_real_directory(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Write real files named by real slugs, then read the directory back.

    Filesystems disagree about the normalization form they STORE (APFS and ext4 keep the bytes
    given; HFS+ decomposed; NTFS keeps UTF-16 as given), so the assertion is equality **after**
    NFC normalization — not raw byte equality, which is not a portable property.
    """
    names = [
        safe_slug_component("한글 메모"),
        safe_slug_component(unicodedata.normalize("NFD", "한글 메모")),
        safe_slug_component("café notes"),
        safe_slug_component("日本語/メモ"),
        safe_slug_component("plain-ascii"),
    ]
    assert all(names), "every fixture must survive the slugger"
    assert names[0] == names[1], "NFD and NFC seeds must name the SAME file"

    for name in names:
        (tmp_path / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")

    on_disk = {unicodedata.normalize("NFC", p.name) for p in tmp_path.iterdir()}
    assert on_disk == {f"{n}.md" for n in set(names)}

    for name in set(names):
        matches = [
            p for p in tmp_path.iterdir() if unicodedata.normalize("NFC", p.name) == f"{name}.md"
        ]
        assert len(matches) == 1
        assert matches[0].read_text(encoding="utf-8") == f"# {name}\n"


def test_decomposed_name_on_disk_round_trips_only_after_nfc_normalization(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A filesystem that stores bytes verbatim (APFS, ext4) can hand back a **decomposed** (NFD)
    name even though every component this module *produces* is NFC (rule 1). This pins the
    caller-facing consequence: a directory listing must be NFC-normalized before it is compared
    against — or checked with :func:`is_safe_component` — because the raw bytes read back from
    disk are not guaranteed to already be in canonical form.
    """
    name = safe_slug_component("café notes")
    nfd_name = unicodedata.normalize("NFD", name)
    (tmp_path / f"{nfd_name}.md").write_bytes(b"# note\n")

    (stored,) = list(tmp_path.iterdir())
    if stored.stem == name:
        return  # this filesystem normalizes to NFC on write (e.g. HFS+); nothing to prove here
    assert stored.stem == nfd_name
    assert not is_safe_component(stored.stem)  # raw bytes: rejected until NFC-normalized
    assert unicodedata.normalize("NFC", stored.stem) == name
    assert safe_slug_component(unicodedata.normalize("NFC", stored.stem)) == name


def test_slugged_component_never_escapes_its_parent_directory(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A component-level sanity check — NOT a substitute for the write-site containment check.

    ``safe_slug_component`` is a charset rule; containment is ``resolve()`` +
    ``is_relative_to()`` and lives at the write site. This only proves the charset rule alone
    cannot produce a component that walks out of a directory.
    """
    root = (tmp_path / "wiki").resolve()
    root.mkdir()
    for raw, _ in HOSTILE_CASES:
        name = safe_slug_component(raw)
        if not name:
            continue
        target = (root / name).resolve()
        assert target.is_relative_to(root)
        assert target.parent == root
