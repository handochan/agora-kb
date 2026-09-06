"""Unicode-safe single path components — the closed-allowlist slugger (Stratum UNIT 1).

Today the curator derives note filenames through two *ASCII* gates: the PATH/ALLOWLIST safe-token
regex ``\\A[A-Za-z0-9][A-Za-z0-9._-]*\\Z`` (:mod:`agora_kb.curator.plan`) and the lowercase slugger
in :mod:`agora_kb.adapters.ollama_brain`. Both are closed sets, which is exactly why they are safe
— and also why a purely non-ASCII seed (a Korean theme title, say) slugifies to ``""`` and falls
through to the ``note-<sha8>`` floor (#57). This module is the **smallest Unicode-safe
replacement** for that charset decision: it widens the admitted characters *without* weakening the
closed-set property, because the widening is a Unicode-**category** ALLOWLIST rather than a
denylist of the characters that happen to be dangerous today.

The allowlist predicate is the one already written for the search tokenizer at
``core/wiki.py:189`` (``unicodedata.category(ch)[0] in ("L", "N")``) plus combining marks ``M``
and the three literal punctuation characters ``-``, ``_`` and ``.``. Everything outside that set
is a *separator*: whitespace, ``/``, ``\\``, NUL, the C0/C1 controls, bidi overrides (U+202E),
zero-width characters (U+200B, U+FEFF), the fullwidth solidus (U+FF0F) and the Windows-hostile
``<>:"|?*`` are all unreachable **without being enumerated**, which is the property that makes an
allowlist strictly stronger than a denylist: a codepoint added by a future Unicode revision is
excluded by default.

**This module has no call sites yet.** It is landed first, on its own, so the property set is
proved in isolation before ``plan.py`` / ``ollama_brain.py`` / ``vault_import.py`` are swapped onto
it; a later failure is then unambiguously attributable to the swap and not to this file.

What this module is NOT
-----------------------
* It does **not** check symlinks. Symlink prevention lives in the curator's FINAL-DIFF gates in
  ``curator/worker.py``'s ``_is_engine_written_raw`` (the ``raw/`` authorship check) and
  ``_assert_final_diff_allowlisted`` (the A/M/R symlink reject). A character rule cannot see an
  inode.
* It does **not** replace containment. A component that is *individually* safe still has to land
  inside the worktree: that is ``resolve()`` + :meth:`pathlib.Path.is_relative_to` at the write
  site, and it must stay there. Character filtering and containment close different holes and
  neither subsumes the other.
* It does **not** decide case. See :func:`safe_slug_component`'s note on lowercasing.
* It does **not** defend against homographs. Cyrillic ``а`` (U+0430) and Latin ``a`` (U+0061) are
  both ``Ll`` and both survive, so they produce two distinct files that look identical. That is a
  stated residual: confusable detection is a different control (and one that cannot live in a
  per-component pure function), and folding it in here would make the allowlist open-ended.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "safe_slug_component",
    "is_safe_component",
    "is_safe_filename_stem",
    "DEFAULT_MAX_BYTES",
]

# ~180 bytes leaves room inside the POSIX NAME_MAX of 255 for a ``.md`` suffix and a ``-2``-style
# collision suffix appended by a caller. The cap is a UTF-8 BYTE cap, not a character count: the
# character slice used by today's ASCII slugger (``ollama_brain.py:345-346``) is only byte-safe
# because its input is ASCII by construction, and a Korean component is 3 bytes per syllable.
DEFAULT_MAX_BYTES = 180

# The literal punctuation admitted alongside the Unicode letter/number/mark categories. ``.`` is
# admitted so an extension survives a round trip, but a *leading* dot is rejected below (no
# dotfiles) and a *trailing* dot is stripped (Windows strips them silently, which would otherwise
# let ``foo.`` and ``foo`` name the same file).
_EXTRA_ALLOWED = "-_."

# The separator every rejected codepoint collapses to. Chosen to match the existing ASCII sluggers
# so an ASCII input the current regexes accept is returned unchanged (idempotency).
_SEP = "-"

# Windows reserved device names. These are reserved as the *stem*, with or without an extension:
# ``con``, ``CON.md`` and ``lpt9.txt`` all resolve to a device, not a file, so a repo cloned to
# Windows would silently fail to check them out. Today's ASCII regex admits every one of them.
# Per Microsoft's "Naming Files, Paths, and Namespaces", the device set is COM0-COM9/LPT0-LPT9
# (COM0/LPT0 included — not just COM1-9/LPT1-9), and the Win32 device-name parser also resolves
# the superscript digit forms COM¹/COM²/COM³ and LPT¹/LPT²/LPT³; ``_is_reserved`` below folds
# those to 1/2/3 before the stem lookup so this set only needs the plain digits.
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(10)} | {f"LPT{i}" for i in range(10)}
)

# Win32 resolves these superscript digits in a device stem the same as the plain digit.
_SUPERSCRIPT_DIGITS = {"¹": "1", "²": "2", "³": "3"}

# The LEGACY writer charset, mirrored from ``core/layout.py``'s ``_WRITER_RE``/``_WRITER_MAX``. It
# is copied rather than imported because the dependency runs the other way (``layout`` imports this
# module), and a lazy import inside the predicate would hide the duplication instead of removing
# it. ``tests/core/test_index_cache.py`` pins the two definitions together so a change to either
# one fails loudly rather than drifting. Used ONLY by :func:`is_safe_filename_stem` (DRILLDOWN-169
# D17) — the write-side namespace guard stays in ``layout.validate_writer``.
_LEGACY_WRITER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_LEGACY_WRITER_MAX = 128


def _is_allowed(ch: str) -> bool:
    """True if ``ch`` may appear literally in a component (the closed category allowlist)."""
    return unicodedata.category(ch)[0] in ("L", "N", "M") or ch in _EXTRA_ALLOWED


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate ``text`` to at most ``max_bytes`` UTF-8 bytes on a character boundary.

    A local twin of ``adapters.ollama_brain._truncate_utf8`` — ``core`` must not import from
    ``adapters`` (the dependency runs the other way), and the six lines are cheaper than the
    coupling.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    clipped = encoded[:max_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8")
        except UnicodeDecodeError:
            clipped = clipped[:-1]  # back off to the last complete character
    return ""


def _trim_edges(text: str) -> str:
    """Strip the separator and the Windows-hostile trailing space/dot from both ends.

    Run to a **fixed point**: the two strips feed each other (``"x.-.-."`` → ``"x.-.-"`` →
    ``"x.-."`` → … → ``"x"``), so a single pass of each leaves a trailing ``.`` behind and breaks
    idempotency. Each iteration strictly shortens the string, so the loop terminates.
    """
    previous = None
    while text != previous:
        previous = text
        # Windows strips trailing spaces and dots from filenames; a space cannot survive the
        # allowlist (Zs is a separator), but the rstrip is kept explicit so the rule reads as
        # written and stays correct if the allowlist ever changes.
        text = text.strip(_SEP).rstrip(" .")
    if text.startswith("."):  # no dotfiles, and "."/".." can only arrive this way
        return ""
    return text


def _is_reserved(text: str) -> bool:
    """True if ``text``'s bare stem is a Windows reserved device name (case-insensitively)."""
    stem = text.split(".", 1)[0].upper()
    for superscript, digit in _SUPERSCRIPT_DIGITS.items():
        stem = stem.replace(superscript, digit)
    return stem in _WINDOWS_RESERVED


def safe_slug_component(token: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Return ``token`` as a filesystem-safe **single** path component, or ``""`` if none remains.

    The rules, applied in order:

    1. NFC-normalize first (the ``core/hashing.py:26`` / ``core/wiki.py:205`` idiom), so a
       macOS-decomposed (NFD) Korean or accented name and its composed form yield the *same*
       component. Filesystems disagree about the form they store; normalizing here means the
       component we compute is the one a caller can compare against after re-reading a directory.
    2. Keep a codepoint only if it is a letter, number or combining mark, or one of ``-_.``;
       every other codepoint becomes a separator, and a run of separators collapses to a single
       ``-``.
    3. Strip leading/trailing ``-`` and trailing spaces and dots — to a fixed point, since the two
       strips feed each other — then reject a leading ``.`` (no dotfiles; this is also what kills
       ``.`` and ``..``).
    4. Reject the Windows reserved bare stems (``CON``, ``PRN``, ``AUX``, ``NUL``, ``COM0``–
       ``COM9``, ``LPT0``–``LPT9``, including the superscript ``COM¹``/``COM²``/``COM³`` and
       ``LPT¹``/``LPT²``/``LPT³`` device forms) case-insensitively, with or without an extension.
    5. Enforce ``max_bytes`` as a **UTF-8 byte** cap with codepoint-safe truncation, then re-trim
       the edges and re-check rule 4 — truncation can expose a new trailing ``-``/``.`` and can
       turn ``console`` into ``con``, so the two rules that a shortened tail can violate are
       re-run rather than assumed.

    Returning ``""`` on total rejection is deliberate: it is the signal that keeps the existing
    ``note-<sha8>`` fallback (``adapters/ollama_brain.py:352-368``, #57) alive as the last-resort
    no-loss floor. This function never raises and never invents content.

    **Case is not folded.** The ASCII sluggers in use today lowercase (``ollama_brain.py`` builds
    ``[a-z0-9-]`` names), but case folding is not this function's job: it is lossy, it is
    locale-sensitive for non-ASCII (Turkish dotless ``ı``, Greek final sigma), and on the
    case-insensitive filesystems that matter here it does not buy collision safety anyway. The
    caller decides — a caller that wants the historical ASCII behaviour lowercases *before*
    calling.

    :param token: the raw text to reduce to one path component.
    :param max_bytes: UTF-8 byte ceiling for the result; ``<= 0`` yields ``""``.
    :returns: a safe component, or ``""`` when nothing safe survives.
    """
    if max_bytes <= 0:
        return ""

    normalized = unicodedata.normalize("NFC", token)

    out: list[str] = []
    for ch in normalized:
        if _is_allowed(ch):
            out.append(ch)
        elif out and out[-1] != _SEP:
            out.append(_SEP)
    candidate = _trim_edges("".join(out))
    if not candidate or _is_reserved(candidate):
        return ""

    candidate = _trim_edges(_truncate_utf8(candidate, max_bytes))
    if not candidate or _is_reserved(candidate):
        return ""
    return candidate


def is_safe_component(token: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    """True if ``token`` is already the canonical form of itself — i.e. needs no rewriting.

    Equivalent to ``safe_slug_component(token) == token`` with one guard: the empty string is
    ``safe_slug_component``'s *rejection sentinel*, not a usable path component, so ``""`` is
    reported unsafe rather than trivially canonical.
    """
    return token != "" and safe_slug_component(token, max_bytes=max_bytes) == token


def is_safe_filename_stem(value: str) -> bool:
    """Is ``value`` safe as the stem of a DERIVED cache filename — the union predicate (D17, #167).

    ``is_safe_component`` (this module's rules) **or** the legacy writer charset
    (``core/layout.py``'s ``_WRITER_RE``, 1-128 chars). Only for the stem of
    ``_kb/index/<stem>.notes.json``; it is **not** a write-namespace guard — an inbox writer or a
    harvest cursor still goes through ``layout.validate_writer`` / ``layout.safe_path_component``,
    which are unchanged.

    Why a UNION and not just ``is_safe_component``: the two rulesets each reject names the other
    admits, and both directions matter here.

    * ``is_safe_component`` alone would newly REJECT ``con``/``CON``/``nul``/``com1``/``aux`` (the
      Windows reserved device stems) and ``foo-``/``foo.`` (trailing separator/dot) — every one of
      which addresses a cache file today. ``core/wiki.py`` swallows the resulting
      ``InvalidWriterError`` and falls back to a full scan, so the regression would be a *silent*
      performance loss with no operator-visible error.
    * The legacy regex alone rejects every non-ASCII name (``내지식``, ``café``) and anything over
      128 characters — which is issue #167 itself.

    The union is therefore purely additive: no repo that has a cache today loses one, and
    non-ASCII repo directories gain one. Path traversal stays refused by BOTH halves (``../escape``,
    ``.hidden``, ``""``, and anything containing a separator fail each rule independently), and
    neither half ever REWRITES: this predicate answers yes/no about ``value`` as given, which is
    what keeps the layout guard from inventing a stem (``safe_slug_component('/etc/passwd')`` would
    return ``'etc-passwd'`` — a different repo's cache file).

    :param value: the candidate filename stem, exactly as it will be interpolated into the path.
    :returns: ``True`` if ``value`` may be used verbatim as a single filename stem.
    """
    if not isinstance(value, str) or value == "":
        return False
    if is_safe_component(value):
        return True
    return bool(_LEGACY_WRITER_RE.match(value)) and len(value) <= _LEGACY_WRITER_MAX
