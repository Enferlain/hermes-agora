# Stores package promotion

16 October 2026 · Resolves the stores row of the 2026-10-11 package-structure convention

## What changed

`agora/stores.py` (1,590 lines) promoted to the `agora/stores/` package exactly as the
convention pre-planned ("promotes when M3 dispatch work lands: core.py, transport.py,
ingestion.py" — dispatch work landed in commit 7528723):

- `_base.py` — exceptions, connection setup with schema checking, the BEGIN IMMEDIATE
  transaction helper, `_Domain`, `SCHEMA_VERSION`.
- `core.py` — `CORE_SCHEMA_DDL` + `CoreStore`.
- `transport.py` — `TRANSPORT_SCHEMA_DDL`, `JournalEntry`, `TransportStore`.
- `ingestion.py` — work/event planning dataclasses (`WorkSeed`, `EventBinding`, ...),
  `default_work_planner`, `ingest_batch`, episode binding, `check_invariants`.
- `__init__.py` — re-exports the entire public surface. The old module also leaked
  `WorkState` through its contracts import; that alias is re-exported deliberately so
  existing `agora.stores.WorkState` accesses keep working.

Mechanical move only — no behavior changes. 315 passed / 0 skipped via `uv run pytest`;
ruff, format, and ty clean.

## Still open

`context_memory.py` crossed the ~1,500-line trigger (1,541) the same week. Its split
shape is a new design decision (the cleanly separable `AuthorityEvaluator` concern vs.
splitting the `ContextMemory` class itself) and is proposed separately rather than
bundled with this move.
