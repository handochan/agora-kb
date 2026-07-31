"""core — the single internal API. write(inbox) · read(wiki) · repo/tenant · state · schema.

Depends on nothing above it (ADR-0001/0003). Phase 1 implements the write path first: see
:mod:`agora_kb.core.inbox`.
"""

from .atomicio import atomic_write_text, fsync_dir
from .hashing import content_sha256, normalize_body
from .ids import new_event_id
from .inbox import (
    Inbox,
    InboxReturn,
    InboxReturnStatus,
    WriteReceipt,
    failed_event_count,
    iter_failed_events,
    resolve_inbox_return,
    return_event_to_inbox,
)
from .layout import InvalidWriterError, RepoLayout, validate_writer
from .models import Confidence, InboxItem, Kind
from .repo import GitError, Repo
from .state import Counters, CuratorState, StateStore
from .wiki import QueryResult, SearchHit, Wiki

__all__ = [
    "Inbox",
    "WriteReceipt",
    "InboxItem",
    "Kind",
    "Confidence",
    "RepoLayout",
    "validate_writer",
    "InvalidWriterError",
    "content_sha256",
    "normalize_body",
    "new_event_id",
    "CuratorState",
    "Counters",
    "StateStore",
    "Repo",
    "GitError",
    "atomic_write_text",
    "fsync_dir",
    "Wiki",
    "SearchHit",
    "QueryResult",
    "failed_event_count",
    "iter_failed_events",
    "InboxReturn",
    "InboxReturnStatus",
    "resolve_inbox_return",
    "return_event_to_inbox",
]
