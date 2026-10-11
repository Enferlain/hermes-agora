# Package structure convention

11 October 2026

How the `agora` package grows without dumping everything into the root or into single ever-growing files. This note sets the standing convention; it changes no code today.

## Rules

1. **The handoff §2 boundaries are the top-level names, permanently.** contracts, stores, context_memory, coordinator, commit_gate, fake_sidecar, ports, runner (plus `cli`). Renames or mergers of these need an architecture-note-level reason, not tidiness.
2. **A module promotes to a package when it actually grows**, not speculatively. Trigger: roughly 1,500+ lines, or three-plus clearly distinct internal concerns. When it promotes, its `__init__.py` re-exports the entire public surface, so `from agora.contracts import X` never breaks — the import path is part of the module's contract.
3. **New M2/M3 concerns go to a §2 owner first.** Access evaluation and transforms belong to context_memory; review routing belongs to commit_gate. Only a concern with no §2 owner (e.g. a replay evidence/report generator for M3 acceptance) gets a new top-level module named after the concern.
4. **No `src/` migration.** The flat `agora/` package at the repo root is the M0 packaging decision (see the 2026-10-09 note); moving to `src/` now would churn every import for no behavioral gain.
5. **Tests mirror concerns, not files** (`test_<concern>.py`); fixture data files live under `tests/fixtures/` once they stop being inline literals.

## Current sizes and planned promotions

| Module | Lines (approx.) | Plan |
|---|---|---|
| `contracts.py` | 2,500 | First promotion, at M2 start: `contracts/` package — `enums.py`, `values.py`, `canonical.py`, `records.py` (§4 families), `codec.py`, `model_steps.py`, re-exporting `__init__.py`. |
| `stores.py` | 1,100 | Promotes when M3 dispatch work lands: `core.py`, `transport.py`, `ingestion.py`. |
| `coordinator.py` | 700 | Stays a module through M2; revisit at M3 if scheduling grows. |
| `context_memory.py`, `commit_gate.py` | < 350 each | Grow in place during M2; promote only on the trigger. |
| `ports.py`, `fake_sidecar.py`, `runner.py`, `cli.py` | < 300 each | Stay modules. |

## Why packages-with-re-export over more flat modules

Splitting `contracts` into sibling top-level modules (`agora_records.py`, `agora_enums.py`, …) would explode the root and break the handoff's ownership vocabulary; one giant module would keep growing through M2/M3 record-adjacent additions. A package keeps the boundary name, caps file size, and leaves the import surface untouched.
