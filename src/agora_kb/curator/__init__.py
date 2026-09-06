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
from .requeue import (
    ArchivedRecord,
    KeptRecord,
    RequeueItem,
    RequeueOutcome,
    RequeueReport,
    Selector,
    StateUnreadable,
    run_requeue,
)
from .restamp import (
    NoteChange,
    RestampPlan,
    RestampReport,
    TagMatch,
    TagSource,
    plan_restamp,
    run_restamp,
)
from .subprocess_backend import BackendUnavailableError, SubprocessBackend
from .triggers import TriggerConfig, TriggerDecision, TriggerReason, evaluate
from .worker import (
    Backend,
    FakeBackend,
    HarvestCursorDelta,
    RunFailure,
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
    # `agora requeue` — the _kb/failed/ → _kb/inbox/ back-edge (issue #99, ADR-0002 appendix).
    "ArchivedRecord",
    "KeptRecord",
    "RequeueItem",
    "RequeueOutcome",
    "RequeueReport",
    "Selector",
    "StateUnreadable",
    "run_requeue",
    # `agora repo upgrade --restamp` — the engine-only frontmatter backfill (#175/#174, ADR-0010
    # §5.2's admin path for the `--tags-from-vault` half).
    "NoteChange",
    "RestampPlan",
    "RestampReport",
    "TagMatch",
    "TagSource",
    "plan_restamp",
    "run_restamp",
    "Candidate",
    "BundleResult",
    "build_bundle",
    "Backend",
    "FakeBackend",
    "HarvestCursorDelta",
    "compute_harvest_cursor_deltas",
    "RunFailure",
    "RunReport",
    "run",
    "recover",
    "SubprocessBackend",
    "BackendUnavailableError",
]
