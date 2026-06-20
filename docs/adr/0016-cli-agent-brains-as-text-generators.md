# ADR-0016 — Headless CLI agents as curator brains (text-generator shim)

**Status:** Accepted · 2026-06-20

Depends on ADR-0004 (pluggable adapters — the swappable write-adapter brain) and ADR-0008/ADR-0011
(the deterministic transaction + INGEST contract that re-grades every brain output). Companion to the
existing Ollama brain shim (`src/agora_kb/adapters/ollama_brain.py`).

## Context
ADR-0004 wants the curator's brain to be a swappable write adapter — explicitly including headless
CLI agents (`claude -p`, `codex exec`, `gemini -p`, `opencode run`, `hermes chat`). The shipped
`ollama_brain` shim covers local Ollama models, but those agents had no adapter.

The tempting approach — let the agentic CLI use its OWN file tools to read the PASS-1 `bundle/`, emit
`plan.json`, and edit the PASS-2 file in place — runs into three real problems:
1. **Trust / safety.** File-read/-write tools in a headless run need elevated permissions
   (`--dangerously-skip-permissions` / broad `--allowedTools`); that is exactly the kind of
   permission-disabled autonomous agent loop a sane environment refuses to spawn.
2. **Worktree pollution.** A real CLI agent (Claude Code, Gemini CLI) is a *stateful* process: it
   writes session/state artifacts (`.omc/`, `.claude/`) into its working directory. With cwd set to
   the curator worktree, those files land in the diff and trip the ADR-0008 FINAL-DIFF allowlist.
3. **Impure output.** An agent told to "edit the file in place" interprets it literally — it tries to
   write files, asks for approval, or prefaces the body with commentary ("Here is the note body:").

## Decision
Use any headless CLI agent as a **pure text generator**, behind a generic shim
(`src/agora_kb/adapters/cli_agent_brain.py`, entry point `agora-cli-brain`) that REUSES the Ollama
brain's robust two-pass pipeline and swaps **only the inference call** from an Ollama HTTP request to
a subprocess:

1. **Reuse, don't reimplement.** `ollama_brain.run_plan` / `run_author` gained a pluggable `infer`
   seam (a `prompt -> text` callable). The shim still reads the bundle FOR the agent, asks ONLY for
   the semantic decision, normalizes the result into a valid-by-construction plan (PASS 1), and
   fills the body-sentinel regions with sanitized prose (PASS 2). The agent never touches a file.
2. **Tool-agnostic by config** (invariant 6): the registry's `adapters.yaml` provides the exact CLI
   argv after a `--` separator, e.g. `argv: [agora-cli-brain, --, claude, -p]`. The configured CLI
   must read its prompt from **stdin** and print its text answer to **stdout**.
3. **No file tools, no skip-permissions.** The agent only generates text, so it needs no elevated
   trust. As always, the worker re-grades every output deterministically OUTSIDE the shim (ADR-0011
   §4): a malformed or adversarial response is caught downstream, never trusted.
4. **Scratch cwd.** The shim runs the agent in a fresh THROWAWAY temp directory, never the worktree —
   so the agent's session/state artifacts can't pollute the diff (problem 2). The shim already read
   the bundle, so the agent needs no worktree access.
5. **Prose-only PASS-2 (`text_only`).** For a text-generator agent the shim sends a strict
   prose-only author prompt (grounded in the §8.2 SOURCE facts) instead of the worker's "edit the
   file in place" prompt — so the agent returns only the body text (problem 3). PASS-1 stdout impurity
   (a "Findings:" preamble around the JSON) is tolerated by the existing `extract_json_object`.

## Consequences
- **+** Genuinely tool-agnostic: ANY stdin→stdout text CLI (or local model) is a curator brain via
  config, with per-act routing (ADR-0015) across them.
- **+** Safe by construction: text-generation only (no file tools, no skip-permissions), agent
  confined to a scratch cwd, and the deterministic gate still owns the integrity verdict.
- **+** One battle-tested pipeline (bundle read → normalize → sentinel fill) shared by every brain;
  the Ollama path is unchanged (the `infer` seam is additive and defaults to Ollama).
- **−** The shim must know each CLI's *text-generation* invocation (e.g. `codex exec` needs
  `--skip-git-repo-check` + a `--sandbox read-only` posture; `gemini -p ""` reads stdin) — captured
  in the `adapters.yaml` argv, not in code.
- **−** Output quality depends on the agent honoring "JSON only" / "body only"; mitigated by
  `extract_json_object` (PASS 1) and the strict `text_only` template (PASS 2), and ultimately by the
  worker's re-grading (a non-conforming response fails the run cleanly — no data risk).
- **−** External auth / account limits are the operator's (e.g. a Gemini CLI account on an ineligible
  tier cannot authenticate at all); the shim surfaces such failures cleanly as a failed run.

## Live verification (2026-06-20, throwaway repo)
Four distinct real brains consolidated captures end-to-end and published lint-clean: **qwen**
(`qwen3.6:35b-a3b`) and **hermes** (`qwen3.6-hermes`) via `agora-ollama-brain`; **claude** (`claude -p`)
and **codex** (`codex exec`) via `agora-cli-brain` (PASS-1 plan + PASS-2 clean prose). **gemini**
failed cleanly on this host (`IneligibleTierError` — an account limitation, not a shim defect), leaving
the wiki untouched. Per-task routing (ADR-0015) across two of them was verified in one run.
