"""Real end-to-end: ``agora capture`` + ``agora curate`` with a LIVE local Ollama brain.

ADR-0041 D1.4/D4.2, issue #153, task B4. ``tests/curator/test_blob_capture.py`` already pins the
write path end to end with ZERO model (a fake backend drives PLAN/AUTHOR deterministically); this
file is the missing leg — does the whole thing actually work through the real CLI with a real brain
generating real PLAN/AUTHOR output over the sandboxed subprocess boundary.

**Opt-in, and NOT part of the default run.** Two gates, deliberately: the module is marked
``live`` (deselected by ``addopts = "-m 'not live'"`` in ``pyproject.toml``) AND skipped unless
``AGORA_LIVE_E2E=1`` is set. The pre-existing ``ollama_reachable()`` skip is the third and weakest
gate: on the maintainer's own machine the pinned brain IS installed, so a presence check alone
would silently add ~15 minutes of non-deterministic, real-model work to every ``uv run pytest`` —
a different cost class from ``tests/curator/test_isolation.py``'s short sandbox probes, which use
the same reachability helper. Run it on purpose::

    AGORA_LIVE_E2E=1 uv run pytest -m live tests/e2e/test_blob_capture_e2e.py

Under all three gates it still skips cleanly (never fails CI) when Ollama is unreachable on
``127.0.0.1:11434`` or the pinned model is not installed
(:func:`agora_kb.curator.isolation.selftest.ollama_reachable`).

A note on flakiness this file was written against, live: a bare-stub candidate ("no extractable
text; attached", nothing else) is thin enough that a real PLAN pass's DROP-vs-keep judgment call
can go either way run to run, sometimes several times in a row — observed live both dropped
(batched with a second, richer candidate, and repeatedly even in isolated single-candidate runs)
and kept (most single-candidate runs) with byte-identical input. That is the curator doing its
job, not a bug: a 1x1 pixel with no text is legitimately marginal content, and a real brain is
not obliged to keep it every time. This file retries the capture+curate cycle (a fresh inbox
event each time — DROP is a terminal disposition, never re-claimed) a generously bounded number
of times rather than asserting a specific judgment call. See ``_curate_until_kept``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from agora_kb.config import write_default_adapters_yaml
from agora_kb.core.layout import RepoLayout
from agora_kb.curator.isolation.selftest import ollama_reachable
from agora_kb.schema.lint import lint

_MODEL = "qwen3.6:35b-a3b"
_OLLAMA_HOST = "http://127.0.0.1:11434"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MAX_CURATE_ATTEMPTS = 8
_KEEP_OPS = ("CREATE_THEME", "APPEND_DAILY", "MERGE_INTO_THEME", "MARK_CONTESTED")


def _model_installed() -> bool:
    from agora_kb.adapters.ollama_brain import BrainError, list_ollama_models

    try:
        return _MODEL in list_ollama_models(_OLLAMA_HOST, timeout=5.0)
    except BrainError:
        return False


pytestmark = [
    # The marker keeps it out of the DEFAULT selection (pyproject `addopts = "-m 'not live'"`);
    # the env var keeps it out of an explicit `pytest tests/e2e/...` that forgot the marker. Both,
    # because a 15-minute live-brain leg must be impossible to start by accident.
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("AGORA_LIVE_E2E") != "1",
        reason="live model e2e: opt in with AGORA_LIVE_E2E=1 (see the module docstring)",
    ),
    pytest.mark.skipif(
        not ollama_reachable() or not _model_installed(),
        reason=f"Ollama not reachable on 127.0.0.1:11434, or model {_MODEL!r} is not installed",
    ),
]


def _agora_bin() -> str:
    """The ``agora`` console script beside the interpreter running pytest (same venv)."""
    candidate = Path(sys.executable).parent / "agora"
    return str(candidate) if candidate.is_file() else "agora"


def _run(args: list[str], *, cwd: Path, timeout: float = 900.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_agora_bin(), *args], cwd=cwd, capture_output=True, text=True, timeout=timeout
    )


def _make_png_bytes() -> bytes:
    """A tiny but genuinely valid 1x1 white PNG — real non-UTF-8 magic + zlib-compressed IDAT."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        length = len(data).to_bytes(4, "big")
        crc = zlib.crc32(tag + data).to_bytes(4, "big")
        return length + tag + data + crc

    width_height = (1).to_bytes(4, "big") * 2
    ihdr = chunk(b"IHDR", width_height + bytes([8, 2, 0, 0, 0]))  # 8-bit RGB
    raw_scanline = bytes([0, 255, 255, 255])  # filter byte 0 + one white RGB pixel
    idat = chunk(b"IDAT", zlib.compress(raw_scanline))
    iend = chunk(b"IEND", b"")
    return _PNG_MAGIC + ihdr + idat + iend


def _kept(stdout: str) -> bool:
    """Did the run's ``counts:`` line record a disposition other than DROP/NOOP?"""
    for line in stdout.splitlines():
        if line.startswith("counts:"):
            keys = {pair.strip().split("=")[0] for pair in line[len("counts:") :].split(",")}
            return bool(keys & set(_KEEP_OPS))
    return False


def _curate_until_kept(
    *, repo: Path, cwd: Path, png_path: Path
) -> list[subprocess.CompletedProcess[str]]:
    """Capture ``png_path`` + curate, retrying (a FRESH capture each time) until a run keeps it.

    Bounded at :data:`_MAX_CURATE_ATTEMPTS`. Every ``agora curate`` call here must still succeed and
    report ``published`` — a hard failure always propagates immediately; only the DROP-vs-keep
    judgment call is retried.
    """
    runs: list[subprocess.CompletedProcess[str]] = []
    for _attempt in range(_MAX_CURATE_ATTEMPTS):
        r_cap = _run(
            ["capture", "--repo", str(repo), "--file", str(png_path), "--domain", "general"],
            cwd=cwd,
        )
        assert r_cap.returncode == 0, r_cap.stdout + r_cap.stderr
        r_curate = _run(["curate", "--repo", str(repo), "--force"], cwd=cwd)
        assert r_curate.returncode == 0, r_curate.stdout + r_curate.stderr
        assert "published" in r_curate.stdout.lower(), r_curate.stdout
        runs.append(r_curate)
        if _kept(r_curate.stdout):
            break
    return runs


def test_blob_capture_real_brain_e2e(tmp_path: Path) -> None:
    repo = tmp_path / "kb"
    repo.mkdir()

    # write_default_adapters_yaml is idempotent + non-destructive (config.py docstring): calling it
    # here, BEFORE `repo init`, pins adapters.yaml to the installed model; repo init's own call to
    # the same function later is then a documented no-op against the file that already exists.
    write_default_adapters_yaml(RepoLayout(repo), model=_MODEL)

    r_init = _run(["repo", "init", str(repo)], cwd=tmp_path)
    assert r_init.returncode == 0, r_init.stdout + r_init.stderr

    adapters_text = (repo / "adapters.yaml").read_text(encoding="utf-8")
    assert _MODEL in adapters_text, adapters_text

    png_bytes = _make_png_bytes()
    png_path = tmp_path / "pixel.png"
    png_path.write_bytes(png_bytes)
    md_path = tmp_path / "note.md"
    md_path.write_text(
        "# Capture smoke note\n\n"
        "This is a small real markdown file captured for the blob-capture end-to-end test.\n",
        encoding="utf-8",
    )

    # A one-off capture (not curated yet) purely to check the CLI's own report of the extraction
    # outcome: `.png` has no text extractor, so the event body must be the one-line stub, not text.
    r_cap_png_probe = _run(
        ["capture", "--repo", str(repo), "--file", str(png_path), "--domain", "general"],
        cwd=tmp_path,
    )
    assert r_cap_png_probe.returncode == 0, r_cap_png_probe.stdout + r_cap_png_probe.stderr
    assert "body: stub" in r_cap_png_probe.stdout, r_cap_png_probe.stdout

    # --- curate the PNG (its own event each retry) until a real run keeps it --------------------
    # Isolated from the .md capture below: batching a bare stub alongside richer text content makes
    # the DROP call even more likely (observed live), and none of this file's assertions concern the
    # .md note's own disposition — isolating is the faithful, lower-flake choice, not a workaround.
    png_runs = _curate_until_kept(repo=repo, cwd=tmp_path, png_path=png_path)
    assert _kept(png_runs[-1].stdout), (
        f"the real brain DROPped every independent PNG capture across "
        f"{len(png_runs)} curate run(s); last run's stdout:\n{png_runs[-1].stdout}"
    )

    r_cap_md = _run(
        ["capture", "--repo", str(repo), "--file", str(md_path), "--domain", "general"],
        cwd=tmp_path,
    )
    assert r_cap_md.returncode == 0, r_cap_md.stdout + r_cap_md.stderr

    r_curate_md = _run(["curate", "--repo", str(repo), "--force"], cwd=tmp_path)
    assert r_curate_md.returncode == 0, r_curate_md.stdout + r_curate_md.stderr

    layout = RepoLayout(repo)
    png_sha = hashlib.sha256(png_bytes).hexdigest()
    blob_path = layout.blob_dir / png_sha[:2] / f"{png_sha}.png"
    sidecar_path = blob_path.with_name(blob_path.name + ".meta.yaml")

    assert blob_path.is_file(), f"{blob_path} missing after curate"
    assert blob_path.read_bytes() == png_bytes, "materialized blob bytes differ from the original"
    assert sidecar_path.is_file(), f"{sidecar_path} missing after curate"
    sidecar_text_first = sidecar_path.read_text(encoding="utf-8")
    assert f"sha256: {png_sha}" in sidecar_text_first
    assert "ext: png" in sidecar_text_first

    blob_ref = f"raw/_blob/{png_sha[:2]}/{png_sha}.png"
    citing_notes = [
        p for p in layout.wiki_dir.rglob("*.md") if blob_ref in p.read_text(encoding="utf-8")
    ]
    assert citing_notes, f"no wiki note cites {blob_ref}"

    # --- v2 lint clean ----------------------------------------------------------------------
    lint_result = lint(layout)
    assert lint_result.ok, [f"{f.code} {f.path}: {f.message}" for f in lint_result.findings]

    # --- doctor healthy -----------------------------------------------------------------------
    r_doctor = _run(["doctor", "--repo", str(repo)], cwd=tmp_path)
    assert r_doctor.returncode == 0, r_doctor.stdout + r_doctor.stderr

    # --- the bundle text the brain read (now archived under processed/) carries no PNG bytes ---
    # `processed/<date>/<event_id>.md` is the immutable inbox event, committed verbatim; its sibling
    # `_attach/<sha>.png` is a DELIBERATE archival copy of the original delivery
    # (`_move_events_to_processed`'s docstring: "keeps the artefact recoverable beside the event")
    # and is expected to hold the real bytes. Every OTHER file under `processed/` — what a human or
    # the brain would actually read as text — must never contain the PNG payload.
    processed_dir = layout.processed_dir
    assert processed_dir.is_dir()
    offenders = [
        f
        for f in processed_dir.rglob("*")
        if f.is_file() and "_attach" not in f.parts and _PNG_MAGIC in f.read_bytes()
    ]
    assert not offenders, f"PNG bytes leaked into non-attachment processed/ files: {offenders}"
    attach_copy = processed_dir.rglob(f"{png_sha}.png")
    assert any(p.read_bytes() == png_bytes for p in attach_copy), (
        "expected the archival _attach/ copy of the captured PNG under processed/"
    )

    # --- re-capture of the identical bytes: immutability holds regardless of this run's verdict -
    # Whether or not the SECOND event gets kept (a real brain may well treat a byte-identical
    # duplicate as nothing new and DROP it), the blob and its sidecar must never be rewritten — the
    # write side neither touches them on a DROP (no note, nothing materialized) nor on a keep (the
    # re-cite branch in `_materialize_one_blob` reads and re-records the existing bytes verbatim).
    blob_mtime_before = blob_path.stat().st_mtime_ns
    sidecar_bytes_before = sidecar_path.read_bytes()

    r_cap_png_2 = _run(
        ["capture", "--repo", str(repo), "--file", str(png_path), "--domain", "general"],
        cwd=tmp_path,
    )
    assert r_cap_png_2.returncode == 0, r_cap_png_2.stdout + r_cap_png_2.stderr

    r_curate_2 = _run(["curate", "--repo", str(repo), "--force"], cwd=tmp_path)
    assert r_curate_2.returncode == 0, r_curate_2.stdout + r_curate_2.stderr

    assert blob_path.read_bytes() == png_bytes
    assert blob_path.stat().st_mtime_ns == blob_mtime_before, "the blob was rewritten on re-capture"
    assert sidecar_path.read_bytes() == sidecar_bytes_before, (
        "the sidecar was rewritten on re-capture — it must still name the FIRST delivering event"
    )
    if not _kept(r_curate_2.stdout):
        # The deterministic re-cite CODE PATH (as opposed to just immutability, checked above
        # either way) is only exercised when the duplicate is actually cited by a note; a live
        # brain dropping a byte-identical duplicate is a legitimate call, and
        # `tests/curator/test_blob_capture.py::test_...recite...` already pins that exact branch
        # deterministically with a fake backend. Not a failure of this file's contract.
        print(
            "note: the second (duplicate) PNG capture was DROPped by the live brain rather than "
            "re-cited — immutability held; the re-cite branch itself is pinned deterministically "
            "in tests/curator/test_blob_capture.py"
        )

    shutil.rmtree(repo, ignore_errors=True)
