"""Context assembly, scoped memory and authority evaluation (handoff §5/§6).

``agora.context_memory`` has been a package since the module crossed the
structure convention's size trigger (see
docs/implementation-notes/2026-10-11-package-structure.md): the cleanly
separable authority-evaluation engine lives in ``evaluation.py``, the
``ContextMemory`` class in ``memory.py``, and this package re-exports the
entire public surface — the import path is part of the module's contract,
so ``from agora.context_memory import X`` keeps working unchanged.
"""

from __future__ import annotations

from .evaluation import AccessVerdict, AuthorityEvaluator, EvaluationInput
from .memory import (
    MAX_CONTEXT_BYTES,
    MAX_SELECTED_EVENTS,
    ContextMemory,
    MemoryStateError,
    ViewAdmissionError,
)

__all__ = [
    "MAX_CONTEXT_BYTES",
    "MAX_SELECTED_EVENTS",
    "AccessVerdict",
    "AuthorityEvaluator",
    "ContextMemory",
    "EvaluationInput",
    "MemoryStateError",
    "ViewAdmissionError",
]
