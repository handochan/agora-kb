"""ADR-0015 per-act curator brain routing: registry resolution, RoutedBackend dispatch, and the
shared ``build_routed_backend`` helper.

The routable-act key-space is the CLOSED set ``{plan, author}`` — co-extensive with the two methods
of the ``worker.Backend`` Protocol (the only two points a brain is invoked). Anything else under
``routing:`` is a fail-loud config error. Routing only chooses WHICH brain runs each act, never how
its output is validated, so the deterministic integrity boundary is unchanged either way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora_kb.curator.backends import BackendRegistry
from agora_kb.curator.subprocess_backend import (
    RoutedBackend,
    SubprocessBackend,
    build_routed_backend,
)

# Three loopback brains (loopback → the builder needs no OS sandbox, so these tests are
# host-independent). ``argv`` is the only required BackendSpec field; the rest default.
_THREE = """\
backends:
  qwen:   { argv: [agora-ollama-brain], network: loopback }
  claude: { argv: [claude, -p], network: loopback }
  hermes: { argv: [hermes, chat], network: loopback }
default_backend: qwen
"""


def _yaml(routing: str = "") -> str:
    return _THREE + routing


# --- registry resolution -------------------------------------------------------------------------
def test_no_routing_resolves_every_act_to_default() -> None:
    reg = BackendRegistry.from_yaml(_THREE)
    assert reg.resolve("plan").name == "qwen"
    assert reg.resolve("author").name == "qwen"
    # Identity: with no routing both acts resolve to the SAME spec object — the builder's
    # single-backend fast path depends on this `is` holding.
    assert reg.resolve("plan") is reg.resolve("author")
    assert reg.routed_backends() == {"plan": "qwen", "author": "qwen"}


def test_full_routing_pins_each_act() -> None:
    reg = BackendRegistry.from_yaml(_yaml("routing:\n  plan: claude\n  author: hermes\n"))
    assert reg.resolve("plan").name == "claude"
    assert reg.resolve("author").name == "hermes"
    assert reg.routed_backends() == {"plan": "claude", "author": "hermes"}


def test_partial_routing_falls_back_to_default_for_the_omitted_act() -> None:
    reg = BackendRegistry.from_yaml(_yaml("routing:\n  plan: claude\n"))
    assert reg.resolve("plan").name == "claude"
    assert reg.resolve("author").name == "qwen"  # omitted → default_backend


def test_empty_routing_block_behaves_like_no_routing() -> None:
    reg = BackendRegistry.from_yaml(_yaml("routing: {}\n"))
    assert reg.routed_backends() == {"plan": "qwen", "author": "qwen"}


def test_unknown_routing_act_is_fail_loud_with_pointer() -> None:
    # GRAFT (ADR-0014/0015): the message names the routable set + states per-op routing is
    # unsupported, so a misguided `routing: {merge: ...}` gets a forward-pointer, not a bare reject.
    with pytest.raises(ValueError, match="not a routable act"):
        BackendRegistry.from_yaml(_yaml("routing:\n  merge: claude\n"))


def test_routing_to_undefined_backend_is_fail_loud() -> None:
    with pytest.raises(ValueError, match="not among the defined backends"):
        BackendRegistry.from_yaml(_yaml("routing:\n  plan: gemini\n"))


def test_routing_not_a_mapping_is_fail_loud() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        BackendRegistry.from_yaml(_yaml("routing: [plan, author]\n"))


def test_routing_value_not_a_string_is_fail_loud() -> None:
    with pytest.raises(ValueError, match="must name a backend"):
        BackendRegistry.from_yaml(_yaml("routing:\n  plan: [claude]\n"))


def test_resolve_rejects_a_non_routable_act() -> None:
    reg = BackendRegistry.from_yaml(_THREE)
    with pytest.raises(ValueError, match="routable act"):
        reg.resolve("merge")


def test_direct_construction_is_guarded_too() -> None:
    # Validation lives in __init__, so a direct build (bypassing from_yaml) is fail-loud as well.
    reg = BackendRegistry.from_yaml(_THREE)
    specs = {name: reg.get(name) for name in reg.names()}
    with pytest.raises(ValueError, match="not a routable act"):
        BackendRegistry(specs, "qwen", routing={"merge": "qwen"})


# --- RoutedBackend dispatch ----------------------------------------------------------------------
class _RecordingBackend:
    """A minimal ``worker.Backend`` that records which acts it was asked to run."""

    def __init__(self, name: str, calls: list[str]) -> None:
        self._name = name
        self._calls = calls

    def plan(self, bundle_dir: Path) -> str:
        self._calls.append(f"{self._name}.plan")
        return f"{self._name}-plan"

    def author(
        self,
        worktree: Path,
        needs_prose: dict[str, list[str]],
        context: dict[str, object],
    ) -> None:
        self._calls.append(f"{self._name}.author")


def test_routed_backend_delegates_each_act_to_its_own_brain(tmp_path: Path) -> None:
    calls: list[str] = []
    planner = _RecordingBackend("planner", calls)
    authorer = _RecordingBackend("authorer", calls)
    routed = RoutedBackend(plan_backend=planner, author_backend=authorer)

    assert routed.plan(tmp_path) == "planner-plan"  # PASS-1 → planner ONLY
    routed.author(tmp_path, {}, {})  # PASS-2 → authorer ONLY
    assert calls == ["planner.plan", "authorer.author"]


# --- build_routed_backend ------------------------------------------------------------------------
def test_builder_no_routing_returns_a_single_subprocess_backend() -> None:
    backend = build_routed_backend(BackendRegistry.from_yaml(_THREE))
    assert isinstance(backend, SubprocessBackend)
    assert backend.spec.name == "qwen"


def test_builder_routing_returns_a_routed_backend() -> None:
    reg = BackendRegistry.from_yaml(_yaml("routing:\n  plan: claude\n  author: qwen\n"))
    backend = build_routed_backend(reg)
    assert isinstance(backend, RoutedBackend)
    assert backend._plan.spec.name == "claude"  # type: ignore[attr-defined]
    assert backend._author.spec.name == "qwen"  # type: ignore[attr-defined]


def test_builder_same_brain_for_both_acts_collapses_to_single_backend() -> None:
    reg = BackendRegistry.from_yaml(_yaml("routing:\n  plan: claude\n  author: claude\n"))
    backend = build_routed_backend(reg)
    assert isinstance(backend, SubprocessBackend)  # one brain for both acts → not routed
    assert backend.spec.name == "claude"


def test_builder_override_pins_both_acts_and_bypasses_routing() -> None:
    reg = BackendRegistry.from_yaml(_yaml("routing:\n  plan: claude\n  author: hermes\n"))
    backend = build_routed_backend(reg, override="qwen")
    assert isinstance(backend, SubprocessBackend)
    assert backend.spec.name == "qwen"


def test_builder_unknown_override_reports_and_returns_none() -> None:
    messages: list[str] = []
    backend = build_routed_backend(
        BackendRegistry.from_yaml(_THREE), override="gemini", report=messages.append
    )
    assert backend is None
    assert any("unknown backend" in m for m in messages)


def test_builder_fail_closed_when_network_none_act_has_no_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``network: none`` act with no usable OS sandbox + ``allow_reduced_isolation=False`` fails
    closed (None + a clear message), never running unconfined (ADR-0013)."""
    from agora_kb.curator import subprocess_backend as sb
    from agora_kb.curator.isolation import SandboxUnavailable

    def _boom(**_kw: object) -> object:
        raise SandboxUnavailable("no kernel sandbox here")

    monkeypatch.setattr(sb, "select_backend_isolation", _boom)
    reg = BackendRegistry.from_yaml(
        "backends:\n  q: { argv: [x], network: none }\ndefault_backend: q\n"
    )
    messages: list[str] = []
    backend = build_routed_backend(reg, report=messages.append)
    assert backend is None
    assert any("no usable OS sandbox" in m for m in messages)


_TWO = """\
backends:
  loop: { argv: [x], network: loopback }
  hush: { argv: [y], network: none }
default_backend: loop
"""


@pytest.mark.parametrize(
    ("routing", "failed_act"),
    [
        ("routing:\n  plan: hush\n  author: loop\n", "plan"),
        ("routing:\n  plan: loop\n  author: hush\n", "author"),
    ],
)
def test_builder_routed_branch_fails_closed_per_act(
    monkeypatch: pytest.MonkeyPatch, routing: str, failed_act: str
) -> None:
    """The ROUTED branches (plan and author resolve to DIFFERENT specs, one ``network: none``) each
    fail closed when no OS sandbox is available — the routed-only branches the single-backend
    fast-path test cannot reach (ADR-0015 §4)."""
    from agora_kb.curator import subprocess_backend as sb
    from agora_kb.curator.isolation import SandboxUnavailable

    def _boom(**_kw: object) -> object:
        raise SandboxUnavailable("no kernel sandbox here")

    monkeypatch.setattr(sb, "select_backend_isolation", _boom)
    reg = BackendRegistry.from_yaml(_TWO + routing)
    messages: list[str] = []
    backend = build_routed_backend(reg, report=messages.append)
    assert backend is None
    assert any(f"({failed_act})" in m and "no usable OS sandbox" in m for m in messages)


# --- default-brain precedence (ADR-0015): routing[act] → repo default → registry default ---------
def test_builder_repo_default_brain_overrides_registry_default() -> None:
    # No routing: an UNROUTED act honors the repo's default brain (repo.yaml curator.backend,
    # threaded as default_backend), NOT the registry's adapters.yaml default — prior selection.
    reg = BackendRegistry.from_yaml(_THREE)  # adapters default_backend: qwen
    backend = build_routed_backend(reg, default_backend="claude")
    assert isinstance(backend, SubprocessBackend)
    assert backend.spec.name == "claude"


def test_builder_routing_beats_repo_default_brain() -> None:
    reg = BackendRegistry.from_yaml(_yaml("routing:\n  plan: hermes\n"))
    backend = build_routed_backend(reg, default_backend="claude")
    assert isinstance(backend, RoutedBackend)
    # routed act wins; the unrouted act falls to the repo's default brain
    assert backend._plan.spec.name == "hermes"  # type: ignore[attr-defined]
    assert backend._author.spec.name == "claude"  # type: ignore[attr-defined]


def test_builder_override_beats_routing_and_repo_default() -> None:
    reg = BackendRegistry.from_yaml(_yaml("routing:\n  plan: hermes\n  author: hermes\n"))
    backend = build_routed_backend(reg, default_backend="claude", override="qwen")
    assert isinstance(backend, SubprocessBackend)
    assert backend.spec.name == "qwen"  # override pins both acts, bypassing routing + repo default


def test_builder_unknown_repo_default_brain_reports_and_returns_none() -> None:
    reg = BackendRegistry.from_yaml(_THREE)
    messages: list[str] = []
    backend = build_routed_backend(reg, default_backend="gemini", report=messages.append)
    assert backend is None
    assert any("unknown backend" in m for m in messages)


def test_resolve_honors_caller_default_over_registry_default() -> None:
    reg = BackendRegistry.from_yaml(_THREE)  # registry default: qwen
    assert reg.resolve("plan", default="claude").name == "claude"
    assert reg.routed_backends(default="claude") == {"plan": "claude", "author": "claude"}


def test_resolve_routing_beats_caller_default() -> None:
    reg = BackendRegistry.from_yaml(_yaml("routing:\n  plan: hermes\n"))
    assert reg.resolve("plan", default="claude").name == "hermes"  # routed wins
    assert reg.resolve("author", default="claude").name == "claude"  # unrouted → caller default


def test_resolve_unknown_default_raises_keyerror() -> None:
    reg = BackendRegistry.from_yaml(_THREE)
    with pytest.raises(KeyError, match="unknown backend"):
        reg.resolve("plan", default="gemini")


# --- parse / IO failures normalize to ValueError (so the faces' `except ValueError` catches them) -
def test_malformed_yaml_is_a_valueerror_not_a_yaml_error() -> None:
    with pytest.raises(ValueError, match="not valid YAML"):
        BackendRegistry.from_yaml('a: "unterminated')


def test_from_file_unreadable_path_is_a_valueerror(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="could not be read"):
        BackendRegistry.from_file(tmp_path / "does-not-exist.yaml")
