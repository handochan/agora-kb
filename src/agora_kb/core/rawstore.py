"""Read ONE artefact out of ``raw/`` — the read face's only door into the capture tier (#169).

``raw/`` holds the immutable captures a curated note cites in its ``sources:``: the extracted text
of an ingested document (``raw/<domain>/<event-id>.md``) and, since ADR-0041 W2.5, the original
bytes themselves (``raw/_blob/<ab>/<sha256>.<ext>`` plus a ``.meta.yaml`` capture sidecar). Until
this module existed nothing on the read side could open any of it — provenance was accurate but not
followable — so a citation was a dead string in every face (DRILLDOWN-169 §0).

**This module creates nothing, ever.** No ``mkdir``, no ``touch``, no ``open(..., "w")``. It is
reader-class code on the wrong side of invariant 2 (all writes go through the inbox; only the
curator writes the tree), and the ``raw/`` capture tier is written by exactly one thing — APPLY's
final-diff-gated engine pass. A refactor that gives this file a write is a refactor that breaks the
CQRS boundary, not an optimisation.

Why a NEW module instead of reusing :class:`~agora_kb.core.wiki.Wiki` (DRILLDOWN-169 D2)
----------------------------------------------------------------------------------------
:meth:`Wiki.get_note <agora_kb.core.wiki.Wiki.get_note>` is safe because it never composes a path
from its argument: it enumerates the notes it already parsed and compares ``rel_path`` for equality,
so traversal is structurally impossible. That posture **cannot be inherited here**. No enumerator
for ``raw/`` exists, and building one per request is open-ended cost — ``raw/`` is larger than
``wiki/``, contains binaries, and the ADR-0012 §2 reader cache is wiki-notes-only, so there is no
cache layer to amortise an ``rglob`` against. This module therefore composes a path from an
untrusted string and has to earn its own safety, which it does with three gates.

The three gates of :func:`resolve`, in order, none substitutable for another
--------------------------------------------------------------------------
1. **Prefix allowlist** (textual). ``posixpath.normpath(rel) == rel`` *and* it starts with
   ``raw/`` *and* it is not absolute. This is the same sentence ``curator/apply.py`` asserts on the
   write side, whose comment says in as many words: *"ALLOWLIST, distinct from containment and not
   substitutable for it."* It answers WHERE INSIDE, which containment never does.
2. **Containment** (filesystem), against :attr:`RepoLayout.raw_dir`, **not the repo root**. The
   curator's ``_contained`` is root-relative and its own docstring concedes that ``raw/../wiki/…``
   resolves in-tree and passes it. Reused verbatim here, a ``raw/x.md`` symlink pointing at
   ``wiki/people/secret.md`` would launder the human-owned namespace (ADR-0041 D3.3) out through a
   ``raw/`` sentence. Resolving against ``raw_dir`` is what refuses that.
3. **Symlink identity rejection**, on the file itself and on every ancestor down from ``raw_dir``.
   Identity, not resolution — the same posture ``schema/notes.py`` takes when enumerating notes: a
   symlink is skipped because of *what it is*, never graded on where it points. Gate 2 alone would
   happily serve a symlink whose target happens to sit inside ``raw/``, which is a file the curator
   never wrote and the final-diff gate never admitted.

Every failure returns ``None``. There is no exception and no message distinguishing "does not
exist" from "refused", because a caller that can tell those apart is a caller that can probe the
filesystem outside the repo one ``stat`` at a time.

No ``schema_version`` branching lives here on purpose: ``raw/`` is byte-identical across schema 1
and schema 2 (ADR-0041 D1.4 never moves it), which is exactly what keeps every stored ``sources:``
string resolvable after the flip.

What this module does NOT decide
--------------------------------
It is a path/byte primitive, not a policy. Whether an artefact may leave the repo at all — the
undesigned people-namespace egress control (R1 / #166) and the ADR-0027 §8 outbound sentinel
posture — is the faces' business, one layer up. And nothing here may be read as a claim that
``raw/`` content is redacted: only the ``session:`` harvest connector redacts on the way in, while
the ``file:`` connector, web uploads, ``kb_remember`` and ``agora capture`` do not (DRILLDOWN-169
§4 T5). It is likewise NOT a claim that the curator authored the file: ``raw_writes`` is one run's
final-diff allowlist, not a repo-wide invariant, and a human can commit into ``raw/`` directly
(§4 T10).
"""

from __future__ import annotations

import posixpath
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import yaml

from .layout import BLOB_PREFIX, SIDECAR_SUFFIX, RepoLayout

__all__ = [
    "MAX_RAW_TEXT_BYTES",
    "RawRef",
    "resolve",
    "read_text",
    "read_sidecar",
    "web_href",
]

#: Upper bound on the bytes :func:`read_text` will pull into memory for one artefact.
#:
#: The 25 MiB upload cap is a WRITE-side control; the read side had none (DRILLDOWN-169 §4 T9), and
#: a face that renders a captured text dump into a template materialises the whole thing twice over.
#: 1 MiB is far above any extractor output that is genuinely a document and far below the point
#: where a single request costs the process its memory. Over-long content is truncated and SAID to
#: be truncated (the second element of :func:`read_text`'s pair) rather than silently clipped.
MAX_RAW_TEXT_BYTES: int = 1 * 1024 * 1024


@dataclass(frozen=True)
class RawRef:
    """One VALIDATED artefact under ``raw/``. Holding one is proof all three gates passed.

    Frozen because it is a capability, not a description: a caller that could mutate ``path`` after
    validation would be holding a reference that no longer means what :func:`resolve` proved. Every
    reader in this module takes a ``RawRef`` rather than a string for the same reason — there is no
    second door into the bytes that skips the gates.
    """

    #: Repo-relative POSIX path, always starting with ``raw/``. This is the citation string
    #: verbatim as it appears in a note's ``sources:`` — never trimmed, never re-spelled, so the
    #: stored identity (ADR-0041 D3.4) survives the round trip through every face.
    rel_path: str
    #: ``"text"`` | ``"blob"`` | ``"sidecar"`` — see :func:`resolve`.
    kind: str
    #: The absolute path on disk. Composed, then proved contained and symlink-free.
    path: Path
    #: ``st_size`` at resolution time. Advisory: a face may use it to decide whether to render.
    size_bytes: int
    #: For ``kind == "blob"`` only, the repo-relative path of its ``.meta.yaml`` capture sidecar.
    #: ``None`` for every other kind. Its EXISTENCE is not asserted here — :func:`read_sidecar`
    #: re-resolves it through the same three gates and tolerates its absence.
    sidecar_rel_path: str | None = None


def resolve(layout: RepoLayout, rel_path: str) -> RawRef | None:
    """Resolve a repo-relative ``raw/`` citation to a validated :class:`RawRef`, or ``None``.

    ``rel_path`` is UNTRUSTED. It reaches here from a note's ``sources:`` frontmatter (which lint
    L1-8 only ever tested with a bare ``exists()``, so ``/etc/hosts`` and ``../../../../etc/hosts``
    both pass it, and a journal's ``sources:`` is graded by nothing at all), from an MCP tool
    argument, from a URL path segment and from a CLI argument. Nothing upstream has narrowed it.

    The three gates run **in this order, as three separate statements** (DRILLDOWN-169 D2). They are
    deliberately not folded into one boolean expression: each closes a hole the others do not, and a
    single expression is one careless simplification away from losing a layer silently.

    Classification, once the gates pass — sidecar FIRST, because a sidecar also lives under
    :data:`~agora_kb.core.layout.BLOB_PREFIX` and is not a blob:

    * ends with ``.meta.yaml`` → ``"sidecar"``. Resolvable so a caller can tell "that is a sidecar"
      apart from "that does not exist"; the faces turn it into a 404 that teaches lint L1-8b's rule
      (cite the artefact, not its sidecar — DRILLDOWN-169 D9).
    * under ``raw/_blob/`` → ``"blob"``, and its sidecar path is composed alongside.
    * otherwise → ``"text"``.

    Returns ``None`` for every failure — refused path, missing file, directory, unreadable ``stat``
    — with no exception and no discrimination between those cases (see the module docstring).
    """
    if not isinstance(rel_path, str) or not rel_path:
        return None

    # --- GATE 1: prefix allowlist (textual). Answers WHERE INSIDE; containment cannot. ----------
    # `normpath(rel) == rel` rejects "raw/../wiki/x.md", "raw/./x.md" and "raw//x.md" by their
    # SPELLING, before any of them is joined to a real directory. Restating apply.py's write-side
    # sentence, whose comment marks it "distinct from containment and not substitutable for it".
    #
    # The NUL test is part of the SAME textual gate and is not decoration: `posixpath.normpath`
    # leaves an embedded NUL untouched and the prefix survives it, so without this the string
    # reaches gate 2, where CPython's `realpath` raises `ValueError: embedded null character in
    # path` — an EXCEPTION out of a function whose whole contract is "every failure returns None"
    # (D2), which the faces then turn into a 500 on an unauthenticated read surface (D3).
    if "\x00" in rel_path:
        return None
    if rel_path.startswith("/") or posixpath.isabs(rel_path):
        return None
    if posixpath.normpath(rel_path) != rel_path:
        return None
    if not rel_path.startswith("raw/"):
        return None

    root = layout.root
    raw_dir = layout.raw_dir
    candidate = root / rel_path

    # --- GATE 2: containment, against raw_dir and NOT the worktree root. ------------------------
    # `resolve()` follows symlinks and absolutises, so this is the layer that catches a component
    # that is a symlink pointing OUT of raw/ (into wiki/people/, into /etc) — and, on Windows, a
    # backslash that posixpath did not treat as a separator in gate 1. Root-relative containment
    # would pass "raw/x.md -> ../wiki/people/secret.md"; this refuses it.
    try:
        resolved = candidate.resolve(strict=False)
        raw_root = raw_dir.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        # RuntimeError: a symlink loop on some platforms. ValueError: belt-and-braces for any
        # byte `realpath` refuses to stat at all (gate 1 already rejects the known one, NUL) —
        # this function's contract is None, never an exception, so nothing here may raise.
        return None
    if not resolved.is_relative_to(raw_root):
        return None

    # --- GATE 3: symlink rejection by IDENTITY, on the file and every ancestor below raw_dir. ---
    # Gate 2 grades where a link POINTS; this grades what the component IS, the posture
    # schema/notes.py takes when enumerating notes. A symlink whose target happens to sit inside
    # raw/ survives gate 2 and is still refused here: it is a file the curator never wrote and the
    # final-diff allowlist never admitted.
    #
    # `raw_dir` ITSELF is tested first, and gate 2 cannot stand in for it: if `raw/` is a symlink,
    # `raw_dir.resolve()` follows the very same link, so both sides of the containment test move
    # together and every path under the target tree "contains". A checkout that carries `raw` as a
    # symlink (git stores symlinks, so it arrives through a clone or a push to a served hub repo)
    # would otherwise turn both read faces into an arbitrary-file server for the target tree — the
    # §4 T3 escape class this gate exists to close. Only raw_dir's OWN identity is graded; its
    # ANCESTORS stay unchecked, because a symlinked worktree (or /tmp -> /private/tmp) is
    # legitimate repo shape and is not something a repo's contents can choose.
    try:
        if raw_dir.is_symlink():
            return None
    except OSError:
        return None
    node = raw_dir
    for part in rel_path.split("/")[1:]:
        node = node / part
        try:
            if node.is_symlink():
                return None
        except OSError:
            return None

    try:
        if not candidate.is_file():
            return None
        size_bytes = candidate.stat().st_size
    except OSError:
        return None

    # Classification is CASEFOLDED, the composed sidecar path is not. On a case-insensitive
    # filesystem (APFS by default, NTFS — and native Windows is a supported target) the gates above
    # happily resolve `raw/_BLOB/<ab>/<sha>.pdf` and `<blob>.META.YAML` to the real files, so a
    # case-SENSITIVE predicate here would classify a blob as "text" (leaking its bytes through the
    # text field, bypassing D5 and the D8 hardened download path) and a sidecar as "blob" (making
    # it directly servable, contradicting D9). Casefolding is strictly a tightening: on a
    # case-sensitive filesystem those spellings do not resolve at all, so no path that reaches here
    # changes kind. `sidecar_rel_path` is still composed from the VERBATIM `rel_path` so the stored
    # identity (ADR-0041 D3.4) is never re-spelled.
    probe = rel_path.casefold()
    if probe.endswith(SIDECAR_SUFFIX.casefold()):
        kind, sidecar_rel_path = "sidecar", None
    elif probe.startswith(f"{BLOB_PREFIX}/".casefold()):
        kind, sidecar_rel_path = "blob", f"{rel_path}{SIDECAR_SUFFIX}"
    else:
        kind, sidecar_rel_path = "text", None

    return RawRef(
        rel_path=rel_path,
        kind=kind,
        path=candidate,
        size_bytes=size_bytes,
        sidecar_rel_path=sidecar_rel_path,
    )


def web_href(rel_path: str) -> str:
    """Map a stored ``raw/…`` citation to the web face's URL for it — the ONE conversion site.

    Decision D6: the web route is mounted at ``/raw``, so the URL drops the stored ``raw/`` prefix
    (keeping it would yield ``/raw/raw/ai-tech/x.md``). The stored identity is never truncated
    anywhere else — ``sources:`` strings, ``kb_read`` and the CLI all keep the full ``raw/…`` form
    (ADR-0041 D3.4); only this href sheds the duplicated segment. ``safe="/"`` keeps path segments
    as segments while escaping spaces / non-ASCII / ``#`` / ``?``.

    It lives in ``core`` — not in the web face — because more than one face needs to SAY the URL:
    the web layer composes ``download_url`` and every ``sources:`` href from it, and the shared
    ``AgoraHandlers.raw()`` seam puts it in a blob payload's ``note`` so an agent (or ``agora
    read``) can hand a human somewhere to get the bytes D5 refuses to ship. Two hand-rolled
    spellings of "drop one segment, percent-encode the rest" agree only by luck — they diverge the
    first time a captured path needs escaping — and D6 exists precisely to keep the rule single.
    ``faces/web/app.py::_raw_href`` is the web-side alias of this function, not a second copy.

    Composing an href is NOT an access decision: the route re-derives servability from the same
    predicate the linkifier used (D12), and :func:`resolve` gates the read independently (D2).
    """
    rest = rel_path[len("raw/") :] if rel_path.startswith("raw/") else rel_path
    return "/raw/" + urllib.parse.quote(rest, safe="/")


def read_text(ref: RawRef, *, max_bytes: int = MAX_RAW_TEXT_BYTES) -> tuple[str, bool]:
    """Read a ``raw/`` artefact as text: ``(text, truncated)``.

    Decoding is ``errors="replace"`` — the tolerant-consumer / strict-producer split of ADR-0014 D1,
    the same posture ``core/wiki.py``'s ``_read_tolerant`` takes for the query path. ``raw/`` holds
    whatever an extractor, an upload or a hand-run capture produced; a single non-UTF-8 byte in one
    captured file must degrade that file's rendering, never fail the read verb.

    At most ``max_bytes`` bytes are pulled into memory (§4 T9) and ``truncated`` says so, so a
    caller can render an honest "…truncated" marker instead of presenting a clipped document as
    whole. Truncation cuts on a BYTE boundary and may therefore land mid-codepoint; the replacement
    character that produces is the tolerant read doing its job.

    ``OSError`` propagates deliberately. This is a genuine I/O failure (the file vanished between
    :func:`resolve` and here, a permission change, a bad disk), not an untrusted-input decision, and
    the house posture is that ``core`` readers tolerate DECODE errors and let I/O errors out —
    swallowing one here would render an unreadable file as an empty one. Faces that owe their
    caller a status dict rather than a traceback wrap the call.
    """
    limit = max(0, max_bytes)
    with ref.path.open("rb") as handle:
        # One byte past the limit: enough to KNOW there was more without holding it.
        data = handle.read(limit + 1)
    truncated = len(data) > limit
    if truncated:
        data = data[:limit]
    return data.decode("utf-8", errors="replace"), truncated


def read_sidecar(layout: RepoLayout, ref: RawRef) -> dict[str, object] | None:
    """Parse a blob's ``.meta.yaml`` capture sidecar, or ``None``.

    The sidecar carries the closed capture key set APPLY writes — ``sha256``, ``ext``,
    ``media_type``, ``bytes``, ``filename``, ``captured_at``, ``writer``, ``source``, ``event_id``,
    with absent optionals OMITTED rather than emitted empty — and never the extracted text. Returned
    as-is: no key is invented, no absent key is filled in, so a caller reporting the capture facts
    reports what was actually recorded.

    Absent, unparseable, not-a-mapping and over-large all return ``None`` rather than raising: the
    sidecar is a description of an artefact, and losing the description must not cost a caller the
    artefact. The size cap is :data:`MAX_RAW_TEXT_BYTES` — a real sidecar is a few hundred bytes,
    and this is the read side's only YAML parse of a git-tracked file that a human can edit.

    The sidecar path goes back through :func:`resolve`, all three gates included, rather than being
    opened straight off ``ref.sidecar_rel_path``: the composed twin of a validated path is not
    itself a validated path (it could be a symlink the blob is not), and one door into ``raw/`` is
    the whole point of this module.
    """
    if ref.kind != "blob" or ref.sidecar_rel_path is None:
        return None
    sidecar = resolve(layout, ref.sidecar_rel_path)
    if sidecar is None or sidecar.size_bytes > MAX_RAW_TEXT_BYTES:
        return None
    try:
        text = sidecar.path.read_text(encoding="utf-8", errors="replace")
        doc = yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError):
        return None
    if not isinstance(doc, dict):
        return None
    return {str(key): value for key, value in doc.items()}
