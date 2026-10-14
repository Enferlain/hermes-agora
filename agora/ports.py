"""Ports for the replay runtime: virtual clock, fault injection, and the
model/review/perception interfaces (handoff §5).

Scripted implementations of the port protocols arrive with the whole-turn
wiring task; this module owns the interfaces and the deterministic
infrastructure they rely on. No wall-clock reads anywhere.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum, StrEnum
from typing import Any, Protocol

from agora.contracts import MainStep, ModelCallEnvelope, check_utc


class ClockError(Exception):
    """The virtual clock was asked to move backwards or read without an anchor."""


class VirtualClock:
    """Virtual monotonic milliseconds, separate from display UTC time.

    Lease/budget/retention transitions use ``now_ms()`` only; display time is
    a pure function of the manifest-supplied anchor plus elapsed virtual
    milliseconds, so a replay with the same manifest is byte-identical. The
    clock never moves backwards within a run (handoff §3).
    """

    def __init__(self, anchor_rfc3339: str, start_ms: int = 0) -> None:
        self._anchor = _parse_utc(anchor_rfc3339)
        if start_ms < 0:
            raise ClockError(f"clock origin cannot be negative (start_ms {start_ms})")
        self._now_ms = start_ms

    def now_ms(self) -> int:
        return self._now_ms

    def advance_ms(self, delta_ms: int) -> int:
        if delta_ms <= 0:
            raise ClockError(f"virtual clock advance requires a positive delta (got {delta_ms}ms)")
        self._now_ms += delta_ms
        return self._now_ms

    def advance_to(self, ms: int) -> int:
        if ms < self._now_ms:
            raise ClockError(f"virtual clock cannot move backwards (to {ms}ms < {self._now_ms}ms)")
        self._now_ms = ms
        return self._now_ms

    def now_rfc3339(self) -> str:
        return _format_utc(self._anchor + timedelta(milliseconds=self._now_ms))

    def persist(self, store: Any) -> None:
        """Write the current virtual ms into the core store's meta table."""
        store.set_meta("clock", self._now_ms)

    @classmethod
    def resume(cls, store: Any, *, anchor_rfc3339: str) -> VirtualClock:
        """Restore the clock from durable state; never below the stored ms."""
        saved = store.get_meta("clock")
        clock = cls(anchor_rfc3339, start_ms=int(saved) if saved is not None else 0)
        return clock


def _parse_utc(value: str) -> datetime:
    check_utc(value, path="anchor_rfc3339")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def _format_utc(moment: datetime) -> str:
    moment = moment.astimezone(UTC)
    if moment.microsecond:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


class CrashCheckpoint(StrEnum):
    """The eight named crash checkpoints (handoff §10)."""

    AFTER_INGEST_BEFORE_ACK = "AFTER_INGEST_BEFORE_ACK"
    AFTER_MODEL_BEFORE_ACCEPT = "AFTER_MODEL_BEFORE_ACCEPT"
    AFTER_RESERVE_BEFORE_LEASE_RETURN = "AFTER_RESERVE_BEFORE_LEASE_RETURN"
    AFTER_LEASE_CONSUME_BEFORE_DISPATCH_START = "AFTER_LEASE_CONSUME_BEFORE_DISPATCH_START"
    AFTER_DISPATCH_START_BEFORE_WORLD = "AFTER_DISPATCH_START_BEFORE_WORLD"
    AFTER_WORLD_BEFORE_RECEIPT = "AFTER_WORLD_BEFORE_RECEIPT"
    DURING_CANCEL = "DURING_CANCEL"
    DURING_CORRECTION_JOB = "DURING_CORRECTION_JOB"


class CrashAction(Enum):
    """What an armed checkpoint does when tripped."""

    RAISE = "raise"


class InjectedCrash(RuntimeError):
    """Simulated process death at a named boundary.

    The crash propagates through transaction context managers (rolling the
    open transaction back); tests catch it, reopen the stores, and continue —
    which is exactly the restart recovery path.
    """

    def __init__(self, checkpoint: CrashCheckpoint) -> None:
        super().__init__(f"injected crash at {checkpoint.value}")
        self.checkpoint = checkpoint


class FaultInjector:
    """Deterministic fault injection: arm a checkpoint, trip it once.

    Flows call ``trip(checkpoint)`` unconditionally; an unarmed checkpoint is
    a no-op, so adding injection points costs one line per boundary.
    """

    def __init__(self) -> None:
        self._armed: dict[CrashCheckpoint, int] = {}

    def arm(self, checkpoint: CrashCheckpoint, *, times: int = 1) -> None:
        if times < 1:
            raise ValueError("times must be >= 1")
        self._armed[checkpoint] = times

    def disarm(self, checkpoint: CrashCheckpoint) -> None:
        self._armed.pop(checkpoint, None)

    def is_armed(self, checkpoint: CrashCheckpoint) -> bool:
        return self._armed.get(checkpoint, 0) > 0

    def armed(self) -> tuple[CrashCheckpoint, ...]:
        return tuple(sorted(self._armed, key=lambda point: point.value))

    def trip(self, checkpoint: CrashCheckpoint) -> None:
        remaining = self._armed.get(checkpoint, 0)
        if remaining <= 0:
            return
        if remaining == 1:
            del self._armed[checkpoint]
        else:
            self._armed[checkpoint] = remaining - 1
        raise InjectedCrash(checkpoint)

    def reset(self) -> None:
        self._armed.clear()


@dataclass(frozen=True, slots=True)
class ReviewAssessmentData:
    """Reviewer port return shape (assessment fields; handoff §4.3)."""

    result: str
    flags: tuple[str, ...]
    runtime_assigned_coverage: str


@dataclass(frozen=True, slots=True)
class PerceptionSignalData:
    """Perception port return shape — signals only, never authority."""

    address_flag: bool
    reply_flag: bool
    status: str
    probabilities: tuple[tuple[str, float | None], ...]


class ScriptedMainModel:
    """Deterministic main port: returns scripted steps in order.

    Determinism rule: the same call index always returns the same step, so
    a crash between model and acceptance replays identically. Running past
    the script is an error, never invention."""

    def __init__(self, steps: Sequence[MainStep]) -> None:
        self._steps = tuple(steps)
        self._calls = 0

    def step(self, envelope: Any) -> MainStep:
        index = self._calls
        self._calls += 1
        if index >= len(self._steps):
            raise RuntimeError(f"scripted main model exhausted after {len(self._steps)} steps")
        return self._steps[index]


class ScriptedReviewer:
    """Deterministic reviewer port (handoff §9): scripted assessments in
    order; the same call index always returns the same result, so a crash
    between review and acceptance replays identically. Running past the
    script is an error, never invention — the gate converts any reviewer
    error into a HOLD, so the script can never manufacture a PASS."""

    def __init__(self, assessments: Sequence[ReviewAssessmentData]) -> None:
        self._assessments = tuple(assessments)
        self._calls = 0

    @property
    def calls(self) -> int:
        """Assessments handed out so far; routing tests assert the NONE
        route never reaches the reviewer."""
        return self._calls

    def assess(self, envelope: Any) -> ReviewAssessmentData:
        index = self._calls
        self._calls += 1
        if index >= len(self._assessments):
            raise RuntimeError(
                f"scripted reviewer exhausted after {len(self._assessments)} assessments"
            )
        return self._assessments[index]


class MainModelPort(Protocol):
    """One deliberation step: envelope in, typed decision out (handoff §5)."""

    def step(self, envelope: ModelCallEnvelope) -> MainStep: ...


class ReviewerPort(Protocol):
    """Assess a review envelope; the runtime fixes coverage, sink, inputs."""

    def assess(self, envelope: Any) -> ReviewAssessmentData: ...


class PerceptionPort(Protocol):
    """Deterministic signals; UNKNOWN falls back to deterministic candidates."""

    def perceive(self, event_refs: Sequence[Any]) -> PerceptionSignalData: ...
