"""The transport domain (handoff §7): the journal, payloads, outbox and
attempts in ``transport.sqlite``; rows are normalized event/payload refs
with no actor or audience enrichment."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._base import (
    RecordConflictError,
    StoreError,
    TransitionError,
    _Domain,
    _tx,
)

TRANSPORT_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS transport_events (
    journal_seq INTEGER PRIMARY KEY,
    source_event_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_ref TEXT NOT NULL,
    fragment_refs TEXT NOT NULL,
    recovery TEXT NOT NULL CHECK (recovery IN ('COMPLETE', 'GAP', 'UNKNOWN')),
    UNIQUE (source_event_id, source_revision)
);

CREATE TABLE IF NOT EXISTS transport_payloads (
    payload_id TEXT PRIMARY KEY,
    owner_ref TEXT NOT NULL,
    data TEXT NOT NULL,
    expires_at_ms INTEGER,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'PURGED'))
);

CREATE TABLE IF NOT EXISTS transport_outbox (
    logical_effect_id TEXT PRIMARY KEY,
    proposal_revision INTEGER NOT NULL,
    proposal_digest TEXT NOT NULL,
    payload_ref TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('QUEUED', 'HELD', 'CANCELLED', 'DISPATCH_STARTED', 'SENT',
                  'FAILED_CONFIRMED', 'UNKNOWN')
    ),
    auth_ref TEXT NOT NULL,
    lease_ref TEXT,
    generation INTEGER NOT NULL,
    target_preconditions TEXT NOT NULL,
    supersession_ack INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transport_attempts (
    attempt_id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL,
    state TEXT NOT NULL,
    fake_remote_id TEXT,
    started_at_ms INTEGER NOT NULL,
    settled_at_ms INTEGER,
    error_code TEXT
);

CREATE TABLE IF NOT EXISTS transport_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """Transport-side journal row view."""

    journal_seq: int
    source_event_id: str
    source_revision: int
    kind: str
    payload_ref: str
    fragment_refs: tuple[str, ...]
    recovery: str


class TransportStore(_Domain):
    """Transport ownership domain: journal, payloads, outbox, attempts, meta."""

    _meta_table = "transport_meta"

    def __init__(self, path: Path | str) -> None:
        super().__init__(Path(path), TRANSPORT_SCHEMA_DDL)
        if self._fresh:
            self._set_meta_tx("transport_generation", 0)
            self._set_meta_tx("consumer_cursor", 0)
            self._set_meta_tx("max_journal_seq", 0)
        else:
            # Sidecar restart advances the durable generation (handoff §3).
            generation = int(self.get_meta("transport_generation") or 0)
            with _tx(self._conn):
                self._set_meta_tx("transport_generation", generation + 1)

    @property
    def transport_generation(self) -> int:
        return int(self.get_meta("transport_generation") or 0)

    @property
    def consumer_cursor(self) -> int:
        return int(self.get_meta("consumer_cursor") or 0)

    @property
    def max_journal_seq(self) -> int:
        return int(self.get_meta("max_journal_seq") or 0)

    def event_exists(self, source_event_id: str, source_revision: int) -> bool:
        """Whether a journal occurrence was already appended — the timer
        fire pass dedupes on this (§8: one occurrence per (intent, due,
        endorsement) tuple, across restarts too)."""
        return (
            self._conn.execute(
                "SELECT 1 FROM transport_events WHERE source_event_id = ? AND source_revision = ?",
                (source_event_id, source_revision),
            ).fetchone()
            is not None
        )

    # -- journal --------------------------------------------------------------

    def append_events(self, entries: tuple[JournalEntry, ...]) -> None:
        """Append journal entries; source event/revision stays unique and
        max_journal_seq tracks the highest seq ever written (gap detection).

        journal_seq is transport-local ordering: appends are contiguous by
        construction, so a hole below max_journal_seq can only mean a
        retained entry no longer exists (deletion under retention) — which
        ingestion records as an explicit GAP. Callers cannot punch holes by
        appending out of order.
        """
        expected = self.max_journal_seq + 1
        with _tx(self._conn) as connection:
            for entry in entries:
                if entry.journal_seq != expected:
                    raise StoreError(
                        f"journal seqs are transport-assigned and contiguous: expected"
                        f" {expected}, got {entry.journal_seq}"
                    )
                expected += 1
                connection.execute(
                    "INSERT INTO transport_events (journal_seq, source_event_id,"
                    " source_revision, kind, payload_ref, fragment_refs, recovery)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.journal_seq,
                        entry.source_event_id,
                        entry.source_revision,
                        entry.kind,
                        entry.payload_ref,
                        json.dumps(list(entry.fragment_refs)),
                        entry.recovery,
                    ),
                )
            highest = max((entry.journal_seq for entry in entries), default=0)
            if highest > self.max_journal_seq:
                self._set_meta_tx("max_journal_seq", highest)

    def events_after(self, cursor: int, limit: int = 64) -> tuple[JournalEntry, ...]:
        rows = self._conn.execute(
            "SELECT * FROM transport_events WHERE journal_seq > ? ORDER BY journal_seq LIMIT ?",
            (cursor, limit),
        ).fetchall()
        return tuple(
            JournalEntry(
                journal_seq=row["journal_seq"],
                source_event_id=row["source_event_id"],
                source_revision=row["source_revision"],
                kind=row["kind"],
                payload_ref=row["payload_ref"],
                fragment_refs=tuple(json.loads(row["fragment_refs"])),
                recovery=row["recovery"],
            )
            for row in rows
        )

    def _insert_gap_marker(self, connection: sqlite3.Connection, seq: int) -> None:
        """Explicit GAP record: a retained journal entry no longer exists."""
        connection.execute(
            "INSERT INTO transport_events (journal_seq, source_event_id, source_revision,"
            " kind, payload_ref, fragment_refs, recovery) VALUES (?, ?, 1, 'CONNECTION_GAP',"
            " '', '[]', 'GAP')",
            (seq, f"gap:{seq}"),
        )

    def acknowledge(self, cursor: int) -> None:
        """Transport-side consumer cursor advance: the acknowledgement."""
        with _tx(self._conn):
            self._set_meta_tx("consumer_cursor", cursor)

    # -- outbox and attempts (basic transitions; full protocol is M3) --------

    def enqueue_effect(
        self,
        *,
        logical_effect_id: str,
        proposal_revision: int,
        proposal_digest: str,
        payload_ref: str,
        auth_ref: str,
        generation: int,
        target_preconditions: tuple[str, ...] = (),
    ) -> None:
        with _tx(self._conn) as connection:
            existing = connection.execute(
                "SELECT proposal_revision, proposal_digest, payload_ref, supersession_ack"
                " FROM transport_outbox WHERE logical_effect_id = ?",
                (logical_effect_id,),
            ).fetchone()
            if existing is not None:
                if existing["proposal_revision"] != proposal_revision:
                    if not existing["supersession_ack"]:
                        raise RecordConflictError(
                            f"effect {logical_effect_id}: revision change without acknowledged"
                            " supersession"
                        )
                    connection.execute(
                        "UPDATE transport_outbox SET proposal_revision = ?, proposal_digest = ?,"
                        " payload_ref = ?, auth_ref = ?, generation = ?, state = 'QUEUED',"
                        " supersession_ack = 0 WHERE logical_effect_id = ?",
                        (
                            proposal_revision,
                            proposal_digest,
                            payload_ref,
                            auth_ref,
                            generation,
                            logical_effect_id,
                        ),
                    )
                    return
                if (
                    existing["proposal_digest"] == proposal_digest
                    and existing["payload_ref"] == payload_ref
                ):
                    return  # exact duplicate submit: current state, no error
                raise RecordConflictError(
                    f"effect {logical_effect_id}: same revision with different bytes is a CONFLICT"
                )
            connection.execute(
                "INSERT INTO transport_outbox (logical_effect_id, proposal_revision,"
                " proposal_digest, payload_ref, state, auth_ref, lease_ref, generation,"
                " target_preconditions, supersession_ack)"
                " VALUES (?, ?, ?, ?, 'QUEUED', ?, NULL, ?, ?, 0)",
                (
                    logical_effect_id,
                    proposal_revision,
                    proposal_digest,
                    payload_ref,
                    auth_ref,
                    generation,
                    json.dumps(list(target_preconditions)),
                ),
            )

    def outbox_state(self, logical_effect_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT state FROM transport_outbox WHERE logical_effect_id = ?",
            (logical_effect_id,),
        ).fetchone()
        return None if row is None else row["state"]

    def acknowledge_supersession(self, logical_effect_id: str) -> bool:
        """Mark that a pending effect revision was durably cancelled before
        dispatch start; returns False when it is too late."""
        with _tx(self._conn) as connection:
            row = connection.execute(
                "SELECT state FROM transport_outbox WHERE logical_effect_id = ?",
                (logical_effect_id,),
            ).fetchone()
            if row is None or row["state"] in ("DISPATCH_STARTED", "SENT", "UNKNOWN"):
                return False
            connection.execute(
                "UPDATE transport_outbox SET supersession_ack = 1, state = 'CANCELLED'"
                " WHERE logical_effect_id = ?",
                (logical_effect_id,),
            )
            return True

    def transition_outbox(self, logical_effect_id: str, new_state: str) -> None:
        legal = {
            ("QUEUED", "HELD"),
            ("QUEUED", "CANCELLED"),
            ("QUEUED", "DISPATCH_STARTED"),
            ("HELD", "QUEUED"),
            ("HELD", "CANCELLED"),
            ("DISPATCH_STARTED", "SENT"),
            ("DISPATCH_STARTED", "FAILED_CONFIRMED"),
            # also restart recovery: a crash past the linearization point
            # with indeterminate world evidence resolves conservatively
            ("DISPATCH_STARTED", "UNKNOWN"),
            # §6.2 reconciliation: only positive world evidence resolves
            # UNKNOWN — APPLIED to SENT, CERTIFIED_NOT_APPLIED to
            # FAILED_CONFIRMED; INDETERMINATE never transitions.
            ("UNKNOWN", "SENT"),
            ("UNKNOWN", "FAILED_CONFIRMED"),
            # §6.2: the single retry path, gated on evidence of no effect plus
            # fresh checks and a new attempt id at dispatch time.
            ("FAILED_CONFIRMED", "QUEUED"),
        }
        with _tx(self._conn) as connection:
            row = connection.execute(
                "SELECT state FROM transport_outbox WHERE logical_effect_id = ?",
                (logical_effect_id,),
            ).fetchone()
            if row is None:
                raise TransitionError(f"effect {logical_effect_id} not in outbox")
            if (row["state"], new_state) not in legal:
                raise TransitionError(
                    f"outbox transition {row['state']} -> {new_state} is not legal"
                )
            connection.execute(
                "UPDATE transport_outbox SET state = ? WHERE logical_effect_id = ?",
                (new_state, logical_effect_id),
            )

    def append_attempt(
        self,
        *,
        attempt_id: str,
        effect_id: str,
        state: str,
        started_at_ms: int,
        fake_remote_id: str | None = None,
    ) -> None:
        """Attempts are immutable inserts (handoff §7 transport.attempts)."""
        with _tx(self._conn):
            self._conn.execute(
                "INSERT INTO transport_attempts (attempt_id, effect_id, state, fake_remote_id,"
                " started_at_ms, settled_at_ms, error_code) VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                (attempt_id, effect_id, state, fake_remote_id, started_at_ms),
            )

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM transport_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return None if row is None else dict(row)
