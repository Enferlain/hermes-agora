"""Two-domain durable storage for the replay runtime (handoff §7).

``agora.stores`` has been a package since M3 dispatch work landed (see
docs/implementation-notes/2026-10-11-package-structure.md): the core,
transport, and ingestion concerns live in their own modules, and this
package re-exports the entire public surface — the import path is part
of the module's contract, so ``from agora.stores import X`` keeps
working unchanged.
"""

from __future__ import annotations

from agora.contracts import WorkState

from ._base import (
    SCHEMA_VERSION,
    DependencyCycleError,
    InvalidReferenceError,
    RecordConflictError,
    SchemaVersionError,
    StoreError,
    TransitionError,
)
from .core import CORE_SCHEMA_DDL, CoreStore
from .ingestion import (
    EventBinding,
    EventPlanner,
    IngestReceipt,
    WorkPlanner,
    WorkSeed,
    check_invariants,
    default_work_planner,
    ingest_batch,
)
from .transport import TRANSPORT_SCHEMA_DDL, JournalEntry, TransportStore

__all__ = [
    "CORE_SCHEMA_DDL",
    "SCHEMA_VERSION",
    "TRANSPORT_SCHEMA_DDL",
    "CoreStore",
    "DependencyCycleError",
    "EventBinding",
    "EventPlanner",
    "IngestReceipt",
    "InvalidReferenceError",
    "JournalEntry",
    "RecordConflictError",
    "SchemaVersionError",
    "StoreError",
    "TransitionError",
    "TransportStore",
    "WorkPlanner",
    "WorkSeed",
    "WorkState",
    "check_invariants",
    "default_work_planner",
    "ingest_batch",
]
