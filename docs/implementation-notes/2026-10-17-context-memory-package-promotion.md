# Context memory package promotion

17 October 2026 · Resolves the context_memory row of the 2026-10-11 convention

## What changed

`agora/context_memory.py` crossed the convention's trigger (1,541 lines, three-plus
concerns) and promoted to the `agora/context_memory/` package as approved (option A of
the 2026-10-16 proposal):

- `evaluation.py` — the cleanly separable authority-evaluation engine: `AuthorityEvaluator`,
  `EvaluationInput`/`AccessVerdict`, and the precision/precedence tables.
- `memory.py` — `ContextMemory` with its exceptions (`MemoryStateError`, `ViewAdmissionError`),
  the selection constants, and the lineage helpers. One evaluator private
  (`_VIEW_KIND_FOR_PRECISION`) is imported across modules for view minting.
- `__init__.py` — re-exports the public surface; every existing
  `from agora.context_memory import X` keeps working.

The `ContextMemory` class itself stays whole: splitting it (snapshot assembly / memory
lifecycle / access wiring) would mean mixin surgery with no second trigger demanding it —
that decision waits for actual growth pressure, per the convention's incremental spirit.

Mechanical move only. 315 passed / 0 skipped via `uv run pytest`; ruff, format and ty clean.
