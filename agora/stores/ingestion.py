"""Journal consumption (handoff §5/§7): the ingest transaction that
stores event refs, plans work, binds events to episode membership, and
advances the contiguous cursor before the transport acknowledgement."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from agora.contracts import (
    ConversationEpisode,
    EventKind,
    InboundEvent,
    Record,
    RecordRef,
    RecoveryStatus,
)
from agora.ports import CrashCheckpoint

from ._base import RecordConflictError
from .core import CORE_SCHEMA_DDL, CoreStore
from .transport import JournalEntry, TransportStore


@dataclass(frozen=True, slots=True)
class WorkSeed:
    """Work to create (or dedupe) for a journal entry."""

    work_id: str
    trigger_key: str
    intent_ref: str
    episode_ref: str | None


WorkPlanner = Callable[[JournalEntry], WorkSeed | None]


@dataclass(frozen=True, slots=True)
class EventBinding:
    """Per-event enrichment an ``InboundEvent`` record needs beyond the
    normalized transport row (handoff §4 InboundEvent, §7 transport.events):
    the actor, the audience the event was observed in, and the episode it
    joins. Supplied by the coordinator/fixture ingest policy. Membership
    confers no scope rights (handoff §5) — it only narrows context."""

    episode_ref: str
    actor_ref: RecordRef
    source_audience_ref: RecordRef
    occurred_at: str | None = None
    reply_target_refs: tuple[RecordRef, ...] = ()
    target_refs: tuple[RecordRef, ...] = ()
    observed_trigger_refs: tuple[RecordRef, ...] = ()


EventPlanner = Callable[[JournalEntry], EventBinding | None]


@dataclass(frozen=True, slots=True)
class IngestReceipt:
    durable_cursor: int
    applied: tuple[int, ...]
    duplicates: tuple[int, ...]
    gap_filled: tuple[int, ...]
    blocked_missing: int | None


def default_work_planner(entry: JournalEntry) -> WorkSeed | None:
    """Mechanical trigger-key mapping; coordinator policy replaces this later."""
    if entry.recovery == "GAP":
        return None
    if entry.kind == "TIMER_DUE":
        prefix = "timer"
    elif entry.kind == "CONNECTION_GAP":
        return None
    else:
        prefix = "turn"
    trigger_key = f"{prefix}:{entry.source_event_id}"
    digest = hashlib.sha256(trigger_key.encode("utf-8")).hexdigest()[:20]
    return WorkSeed(
        work_id=f"work:{prefix}:{digest}",
        trigger_key=trigger_key,
        intent_ref="",
        episode_ref=None,
    )


def check_invariants(core: CoreStore, transport: TransportStore) -> list[str]:
    """Shared store invariant battery for tests and recovery checks.

    Returns violation strings; empty means healthy. Covers the §7 schema
    digest agreement, exactly one current revision per record id, cursor
    contiguity against ingestion rows, and the ack bound.
    """
    violations: list[str] = []
    digest = hashlib.sha256(CORE_SCHEMA_DDL.encode("utf-8")).hexdigest()
    if core.get_meta("schema_digest") != digest:
        violations.append("core schema digest mismatch")
    multi = core._conn.execute(
        "SELECT record_id, count(*) AS n FROM core_records WHERE is_current = 1"
        " GROUP BY record_id HAVING n > 1"
    ).fetchall()
    violations.extend(f"record {row['record_id']} has multiple current revisions" for row in multi)
    cursor = core.cursor
    rows = core._conn.execute(
        "SELECT journal_seq FROM core_ingestion WHERE journal_seq <= ? ORDER BY journal_seq",
        (cursor,),
    ).fetchall()
    for expected, row in enumerate(rows, start=1):
        if row["journal_seq"] != expected:
            violations.append(f"ingestion cursor prefix not contiguous at seq {row['journal_seq']}")
            break
    beyond = core._conn.execute(
        "SELECT count(*) FROM core_ingestion WHERE journal_seq > ?", (cursor,)
    ).fetchone()[0]
    if beyond:
        violations.append("ingestion rows exist beyond the durable cursor")
    if transport.consumer_cursor > cursor:
        violations.append("transport ack cursor is ahead of the core cursor")
    return violations


# ---------------------------------------------------------------------------
# Ingestion: one core transaction, then the checkpoint, then the ack
# (handoff §5 ingest, §7 core ingestion transaction)
# ---------------------------------------------------------------------------


def _bind_event_tx(
    core: CoreStore,
    connection: sqlite3.Connection,
    entry: JournalEntry,
    binding: EventBinding,
    *,
    ingested_at: str,
    bound: list[Record],
) -> None:
    """Bind one journal entry to its episode inside the caller's
    transaction: create the ``InboundEvent`` record when absent, then add
    it to the episode's membership when the episode record exists —
    appended on first sight, followed in place (same position, current
    revision) when the event's upstream revision changes. Every step is
    idempotent, so newly applied entries and crash-replayed duplicates
    take the same path — a replay repairs a first pass whose episode or
    fragment records had not been seeded yet, instead of leaving the
    event permanently outside its episode's context.

    Fail-closed rules: GAP-recovery rows are transport bookkeeping, never
    members; an entry citing a fragment with no core record gets no
    InboundEvent and no membership (the raw ingestion row still lands and
    the cursor still advances — an enrichment gap never blocks journal
    continuity); unknown event kinds are future typed extensions (§4) —
    never silently mapped, so the enum conversion raises."""
    if entry.recovery == RecoveryStatus.GAP.value:
        return
    if core.get_record(entry.source_event_id, entry.source_revision) is None:
        fragment_refs = _bound_fragment_refs(core, entry)
        if fragment_refs is None:
            return
        event = InboundEvent(
            id=entry.source_event_id,
            revision=entry.source_revision,
            created_at=ingested_at,
            source_event_id=entry.source_event_id,
            journal_seq=entry.journal_seq,
            kind=EventKind(entry.kind),
            actor_ref=binding.actor_ref,
            occurred_at=binding.occurred_at or ingested_at,
            ingested_at=ingested_at,
            source_audience_ref=binding.source_audience_ref,
            fragment_refs=fragment_refs,
            reply_target_refs=binding.reply_target_refs,
            target_refs=binding.target_refs,
            observed_trigger_refs=binding.observed_trigger_refs,
            recovery=RecoveryStatus(entry.recovery),
        )
        core._put_record_tx(connection, event)
        bound.append(event)
    episode = core.get_record(binding.episode_ref)
    if not isinstance(episode, ConversationEpisode):
        return
    event_ref = RecordRef(id=entry.source_event_id, revision=entry.source_revision)
    members = episode.member_event_refs
    updated_members: tuple[RecordRef, ...] | None = None
    for index, ref in enumerate(members):
        if ref.id == event_ref.id:
            if ref.revision == event_ref.revision:
                return  # true duplicate: the member is already current
            # the event's upstream revision changed (MESSAGE_UPDATED /
            # MESSAGE_DELETED): follow it in place — same conversational
            # position, current revision — so later turns resolve the
            # edited (or tombstoned) content while snapshots that pinned
            # the episode earlier keep their as-of-turn view
            updated_members = (*members[:index], event_ref, *members[index + 1 :])
            break
    if updated_members is None:
        updated_members = (*members, event_ref)
    updated = replace(
        episode,
        revision=episode.revision + 1,
        member_event_refs=updated_members,
    )
    core._put_record_tx(connection, updated)
    # the episode revision is verified like any other record write, so the
    # member ref cannot dangle even if this path ever changes
    bound.append(updated)


def _bound_fragment_refs(core: CoreStore, entry: JournalEntry) -> tuple[RecordRef, ...] | None:
    """The entry's fragment ids resolved to their current core records —
    an event's content is what it carried when ingested, so the refs pin
    the revision current at ingest time. None when any fragment has no
    core record: the binding is skipped fail-closed rather than raising,
    so a mis-ordered entry can never block the cursor."""
    refs: list[RecordRef] = []
    for fragment_id in entry.fragment_refs:
        ref = core.current_ref(fragment_id)
        if ref is None:
            return None
        refs.append(ref)
    return tuple(refs)


def ingest_batch(
    core: CoreStore,
    transport: TransportStore,
    *,
    consumer_id: str,
    planner: WorkPlanner | None = None,
    event_planner: EventPlanner | None = None,
    ingested_at: str,
    limit: int = 64,
    injector: Any = None,
) -> IngestReceipt:
    """Ingest journal entries past the core cursor.

    One core transaction stores event refs, creates/dedupes work, and
    advances the contiguous cursor — committed BEFORE the transport
    acknowledgement. Entries the journal no longer retains become explicit
    GAP rows; a never-written missing seq blocks the cursor instead.

    With an ``event_planner``, each entry — newly applied or replayed as a
    duplicate — gets its InboundEvent record and episode membership append
    in the same transaction when its policy binds it (bead 93h: context
    selection is episode-scoped through ConversationEpisode.member_event_refs).
    Binding is idempotent, so a crash-replayed duplicate repairs a first
    pass whose episode or fragment records had not been seeded yet.
    """
    plan = planner if planner is not None else default_work_planner
    cursor = core.cursor
    entries = transport.events_after(cursor, limit)
    max_ever = transport.max_journal_seq
    applied: list[int] = []
    duplicates: list[int] = []
    gap_filled: list[int] = []
    blocked_missing: int | None = None
    bound: list[Record] = []

    with core.transaction() as connection:
        for entry in entries:
            if entry.journal_seq > cursor + 1:
                missing = range(cursor + 1, entry.journal_seq)
                never_written = [seq for seq in missing if seq > max_ever]
                if never_written:
                    # Out-of-order arrival: the cursor cannot advance past a
                    # missing seq (handoff §7).
                    blocked_missing = never_written[0]
                    break
                for seq in missing:
                    # Retained entry no longer exists: explicit GAP, never
                    # pretended completeness. The marker write goes to the
                    # transport domain (its own commit), deliberately outside
                    # the core transaction.
                    transport._insert_gap_marker(transport._conn, seq)
                    core._insert_ingestion_row(
                        connection,
                        consumer_id=consumer_id,
                        source_event_id=f"gap:{seq}",
                        source_revision=1,
                        journal_seq=seq,
                        fragment_refs=(),
                        recovery=RecoveryStatus.GAP.value,
                        ingested_at=ingested_at,
                    )
                    gap_filled.append(seq)
                    cursor = seq
            existing = core._ingest_row_exists(
                consumer_id, entry.source_event_id, entry.source_revision
            )
            if existing is not None:
                if existing["journal_seq"] != entry.journal_seq:
                    raise RecordConflictError(
                        f"event {entry.source_event_id} re-ingested at a different journal seq"
                    )
                duplicates.append(entry.journal_seq)
            else:
                core._insert_ingestion_row(
                    connection,
                    consumer_id=consumer_id,
                    source_event_id=entry.source_event_id,
                    source_revision=entry.source_revision,
                    journal_seq=entry.journal_seq,
                    fragment_refs=entry.fragment_refs,
                    recovery=entry.recovery,
                    ingested_at=ingested_at,
                )
                applied.append(entry.journal_seq)
            if event_planner is not None:
                binding = event_planner(entry)
                if binding is not None:
                    # one idempotent path for applied entries and replayed
                    # duplicates: a duplicate repairs a first pass whose
                    # episode or fragment records had not been seeded yet
                    _bind_event_tx(
                        core, connection, entry, binding, ingested_at=ingested_at, bound=bound
                    )
            seed = plan(entry)
            if seed is not None:
                core._create_work_tx(
                    connection,
                    work_id=seed.work_id,
                    trigger_key=seed.trigger_key,
                    intent_ref=seed.intent_ref,
                    episode_ref=seed.episode_ref,
                    runtime_epoch=core.runtime_epoch,
                )
            cursor = entry.journal_seq
        if bound:
            # Verified once after every insert, like put_records: the episode
            # revision can cite the InboundEvent written earlier in the same
            # transaction.
            core._verify_ref_targets_tx(connection, tuple(bound))
        core._set_meta_tx("cursor", cursor)

    if injector is not None:
        injector.trip(CrashCheckpoint.AFTER_INGEST_BEFORE_ACK)

    transport.acknowledge(cursor)
    return IngestReceipt(
        durable_cursor=cursor,
        applied=tuple(applied),
        duplicates=tuple(duplicates),
        gap_filled=tuple(gap_filled),
        blocked_missing=blocked_missing,
    )
