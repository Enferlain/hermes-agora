"""Port-layer tests: virtual clock determinism and fault-injection semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

import agora.stores as stores
from agora.ports import (
    ClockError,
    CrashCheckpoint,
    FaultInjector,
    InjectedCrash,
    VirtualClock,
)


def test_clock_is_monotonic_and_anchored() -> None:
    clock = VirtualClock("2026-10-10T00:00:00Z")
    assert clock.now_ms() == 0
    assert clock.now_rfc3339() == "2026-10-10T00:00:00Z"
    clock.advance_ms(1500)
    assert clock.now_ms() == 1500
    assert clock.now_rfc3339() == "2026-10-10T00:00:01.500000Z"


def test_clock_converts_anchor_relative_times() -> None:
    """The fixed-time defer window conversions (§6.4): at_rfc3339 mints an
    absolute display time at a virtual-ms offset, to_ms reads one back —
    including before the anchor and with fractional seconds."""
    clock = VirtualClock("2026-10-10T12:00:00Z")
    assert clock.at_rfc3339(60_000) == "2026-10-10T12:01:00Z"
    assert clock.at_rfc3339(1_500) == "2026-10-10T12:00:01.500000Z"
    assert clock.to_ms("2026-10-10T12:01:00Z") == 60_000
    assert clock.to_ms("2026-10-10T12:00:01.500000Z") == 1_500
    assert clock.to_ms("2026-10-10T11:59:00Z") == -60_000


def test_clock_cannot_move_backwards() -> None:
    clock = VirtualClock("2026-10-10T00:00:00Z", start_ms=100)
    with pytest.raises(ClockError):
        clock.advance_ms(-1)
    with pytest.raises(ClockError):
        clock.advance_ms(0)  # non-positive deltas fail
    with pytest.raises(ClockError):
        clock.advance_to(50)
    assert clock.now_ms() == 100


def test_clock_rejects_negative_origin() -> None:
    with pytest.raises(ClockError):
        VirtualClock("2026-10-10T00:00:00Z", start_ms=-5)


def test_display_time_is_a_pure_function_of_virtual_ms() -> None:
    first = VirtualClock("2026-01-01T12:00:00Z")
    second = VirtualClock("2026-01-01T12:00:00Z")
    for clock in (first, second):
        clock.advance_ms(61_000)
    assert first.now_rfc3339() == second.now_rfc3339()


def test_clock_persists_and_resumes_never_below_saved(tmp_path: Path) -> None:
    core = stores.CoreStore(tmp_path / "core.sqlite")
    clock = VirtualClock("2026-10-10T00:00:00Z")
    clock.advance_ms(2500)
    clock.persist(core)
    resumed = VirtualClock.resume(core, anchor_rfc3339="2026-10-10T00:00:00Z")
    assert resumed.now_ms() == 2500
    with pytest.raises(ClockError):
        resumed.advance_to(1000)


def test_injector_is_one_shot_and_scoped() -> None:
    injector = FaultInjector()
    injector.trip(CrashCheckpoint.DURING_CANCEL)  # unarmed: no-op
    injector.arm(CrashCheckpoint.DURING_CANCEL)
    with pytest.raises(InjectedCrash) as excinfo:
        injector.trip(CrashCheckpoint.DURING_CANCEL)
    assert excinfo.value.checkpoint is CrashCheckpoint.DURING_CANCEL
    injector.trip(CrashCheckpoint.DURING_CANCEL)  # spent: no-op
    assert not injector.is_armed(CrashCheckpoint.DURING_CANCEL)
    injector.arm(CrashCheckpoint.DURING_CANCEL, times=2)
    injector.reset()
    assert injector.armed() == ()


def test_injector_repeats_when_armed_multiple_times() -> None:
    injector = FaultInjector()
    injector.arm(CrashCheckpoint.DURING_CANCEL, times=2)
    with pytest.raises(InjectedCrash):
        injector.trip(CrashCheckpoint.DURING_CANCEL)
    with pytest.raises(InjectedCrash):
        injector.trip(CrashCheckpoint.DURING_CANCEL)
    injector.trip(CrashCheckpoint.DURING_CANCEL)  # spent
