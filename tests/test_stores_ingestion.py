"""Ingestion tests: the one-transaction commit, crash-before-ack restart,
duplicates, contiguous appends, and GAP semantics (handoff §5, §7)."""

from __future__ import annotations

from pathlib import Path

import pytest

import agora.stores as stores
from agora.ports import CrashCheckpoint, FaultInjector, InjectedCrash

UTC = "2026-10-10T00:00:00Z"
CONSUMER = "core-1"


@pytest.fixture
def stores_pair(tmp_path: Path) -> tuple[stores.CoreStore, stores.TransportStore]:
    return (
        stores.CoreStore(tmp_path / "core.sqlite"),
        stores.TransportStore(tmp_path / "transport.sqlite"),
    )


def entry(seq: int, kind: str = "MESSAGE_CREATED") -> stores.JournalEntry:
    return stores.JournalEntry(
        journal_seq=seq,
        source_event_id=f"evt:{seq}",
        source_revision=1,
        kind=kind,
        payload_ref=f"payload:{seq}",
        fragment_refs=(f"fragment:{seq}",),
        recovery="COMPLETE",
    )


def test_happy_path_applies_work_and_acks(
    stores_pair: tuple[stores.CoreStore, stores.TransportStore],
) -> None:
    core, transport = stores_pair
    transport.append_events((entry(1), entry(2)))
    receipt = stores.ingest_batch(core, transport, consumer_id=CONSUMER, ingested_at=UTC)
    assert receipt.applied == (1, 2)
    assert receipt.durable_cursor == 2
    assert transport.consumer_cursor == 2
    assert core.work_by_trigger_key("turn:evt:1") is not None
    assert core.work_by_trigger_key("turn:evt:2") is not None


def test_crash_before_ack_loses_nothing_and_duplicates_nothing(
    tmp_path: Path,
) -> None:
    core = stores.CoreStore(tmp_path / "core.sqlite")
    transport = stores.TransportStore(tmp_path / "transport.sqlite")
    transport.append_events((entry(1), entry(2)))
    stores.ingest_batch(core, transport, consumer_id=CONSUMER, ingested_at=UTC)

    # Simulated process death between the core commit and the transport ack.
    core.close()
    transport.close()
    core = stores.CoreStore(tmp_path / "core.sqlite")
    transport = stores.TransportStore(tmp_path / "transport.sqlite")
    injector = FaultInjector()
    injector.arm(CrashCheckpoint.AFTER_INGEST_BEFORE_ACK)
    transport.append_events((entry(3),))
    with pytest.raises(InjectedCrash):
        stores.ingest_batch(
            core, transport, consumer_id=CONSUMER, ingested_at=UTC, injector=injector
        )

    # Core committed before the crash; the transport ack did not happen.
    assert core.cursor == 3
    assert transport.consumer_cursor == 2
    assert core.work_by_trigger_key("turn:evt:3") is not None

    # Restart: the retry applies nothing new and creates no duplicate work.
    core.close()
    transport.close()
    core = stores.CoreStore(tmp_path / "core.sqlite")
    transport = stores.TransportStore(tmp_path / "transport.sqlite")
    receipt = stores.ingest_batch(core, transport, consumer_id=CONSUMER, ingested_at=UTC)
    assert receipt.applied == ()
    assert receipt.durable_cursor == 3
    assert transport.consumer_cursor == 3
    # exactly one work row per trigger key
    rows = core._conn.execute("SELECT count(*) FROM core_work").fetchone()[0]
    assert rows == 3
    assert stores.check_invariants(core, transport) == []


def test_stale_cursor_replays_report_duplicates(
    stores_pair: tuple[stores.CoreStore, stores.TransportStore],
) -> None:
    core, transport = stores_pair
    transport.append_events((entry(1), entry(2)))
    stores.ingest_batch(core, transport, consumer_id=CONSUMER, ingested_at=UTC)
    # A stale cursor restore replays already-ingested rows: duplicates, no
    # new work, no conflict.
    core.set_cursor(0)
    receipt = stores.ingest_batch(core, transport, consumer_id=CONSUMER, ingested_at=UTC)
    assert receipt.duplicates == (1, 2)
    rows = core._conn.execute("SELECT count(*) FROM core_work").fetchone()[0]
    assert rows == 2


def test_journal_appends_are_contiguous(
    stores_pair: tuple[stores.CoreStore, stores.TransportStore],
) -> None:
    _core, transport = stores_pair
    transport.append_events((entry(1),))
    with pytest.raises(stores.StoreError):
        transport.append_events((entry(3),))  # hole-punching is refused


def test_deleted_retained_entry_becomes_explicit_gap(
    stores_pair: tuple[stores.CoreStore, stores.TransportStore],
) -> None:
    core, transport = stores_pair
    transport.append_events((entry(1), entry(2), entry(3)))
    with transport.transaction() as connection:
        connection.execute("DELETE FROM transport_events WHERE journal_seq = 2")
    receipt = stores.ingest_batch(core, transport, consumer_id=CONSUMER, ingested_at=UTC)
    assert receipt.gap_filled == (2,)
    assert receipt.applied == (1, 3)
    assert receipt.durable_cursor == 3
    assert stores.check_invariants(core, transport) == []
    marker = transport._conn.execute(
        "SELECT recovery FROM transport_events WHERE journal_seq = 2"
    ).fetchone()
    assert marker["recovery"] == "GAP"
    ingested = core._conn.execute(
        "SELECT recovery FROM core_ingestion WHERE journal_seq = 2"
    ).fetchone()
    assert ingested["recovery"] == "GAP"
    # a gap creates no work
    assert core.work_by_trigger_key("turn:gap:2") is None


def test_revised_source_event_ingests_normally(
    stores_pair: tuple[stores.CoreStore, stores.TransportStore],
) -> None:
    core, transport = stores_pair
    first = entry(1)
    revised = stores.JournalEntry(
        journal_seq=2,
        source_event_id="evt:1",
        source_revision=2,
        kind="MESSAGE_UPDATED",
        payload_ref="payload:evt:1:r2",
        fragment_refs=("fragment:evt:1",),
        recovery="COMPLETE",
    )
    transport.append_events((first, revised))
    receipt = stores.ingest_batch(core, transport, consumer_id=CONSUMER, ingested_at=UTC)
    assert receipt.applied == (1, 2)
    assert receipt.durable_cursor == 2
    assert core.work_by_trigger_key("turn:evt:1") is not None
    assert stores.check_invariants(core, transport) == []


def test_same_event_at_different_seq_is_conflict(
    stores_pair: tuple[stores.CoreStore, stores.TransportStore],
) -> None:
    core, transport = stores_pair
    transport.append_events((entry(1),))
    stores.ingest_batch(core, transport, consumer_id=CONSUMER, ingested_at=UTC)
    # The journal refuses a re-append of the same source event entirely.
    with pytest.raises(stores.StoreError):
        transport.append_events((entry(1),))
