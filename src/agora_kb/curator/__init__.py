"""curator — sleep-time consolidation worker (one per repo) + pluggable write-adapter brains
(adapters.yaml) + triggers (cron/threshold/idle) + candidate review gate (ADR-0002/0004)."""

from .apply import (
    ApplyError,
    apply_plan,
    strip_stray_wikilinks,
    validate_author_diff,
)
from .backends import BackendRegistry, BackendResult, BackendSpec, run_backend
from .bundle import BundleResult, Candidate, build_bundle
from .claim import LockHeld, claim, curator_lock
from .manifest import (
    RunManifest,
    list_processing,
    manifest_path,
    read_manifest,
    write_manifest,
)
from .plan import Disposition, Plan, PlanError, PlanParseError, validate_plan
from .subprocess_backend import BackendUnavailableError, SubprocessBackend
from .triggers import TriggerConfig, TriggerDecision, TriggerReason, evaluate
from .worker import (
    Backend,
    FakeBackend,
    HarvestCursorDelta,
    RunReport,
    compute_harvest_cursor_deltas,
    recover,
    run,
)

__all__ = [
    "TriggerConfig",
    "TriggerDecision",
    "TriggerReason",
    "evaluate",
    "BackendSpec",
    "BackendResult",
    "BackendRegistry",
    "run_backend",
    "Plan",
    "Disposition",
    "PlanError",
    "PlanParseError",
    "validate_plan",
    "apply_plan",
    "validate_author_diff",
    "strip_stray_wikilinks",
    "ApplyError",
    "RunManifest",
    "manifest_path",
    "write_manifest",
    "read_manifest",
    "list_processing",
    "curator_lock",
    "claim",
    "LockHeld",
    "Candidate",
    "BundleResult",
    "build_bundle",
    "Backend",
    "FakeBackend",
    "HarvestCursorDelta",
    "compute_harvest_cursor_deltas",
    "RunReport",
    "run",
    "recover",
    "SubprocessBackend",
    "BackendUnavailableError",
]
