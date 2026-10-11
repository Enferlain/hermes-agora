"""Fake sidecar and fake world for the replay slice (handoff §2, §5, §6.2).

The sidecar owns outbox/attempt dispatch against the transport store; the
fake world is independent durable evidence of remote visibility in its own
SQLite file, readable by core only through the reconciliation port. Neither
owns identity, memory, or policy decisions.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from agora.contracts import (
    CommitAuthorization,
    DeliveryReceipt,
    DeliveryState,
    Effect,
    ReasonCode,
    Record,
    RecordRef,
    canonical_json_bytes,
)
from agora.ports import CrashCheckpoint, FaultInjector, VirtualClock
from agora.stores import CoreStore, TransportStore

WORLD_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS world_effects (
    attempt_id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL,
    payload_bytes TEXT NOT NULL,
    visible_at_ms INTEGER NOT NULL,
    remote_id TEXT,
    outcome TEXT NOT NULL
);
"""

DISPATCH_LEASE_MS = 2000


class SidecarError(Exception):
    """Sidecar-side failure with a safe reason code."""

    def __init__(self, message: str, code: ReasonCode) -> None:
        super().__init__(message)
        self.code = code


class ReconcileResult(StrEnum):
    APPLIED = "APPLIED"
    CERTIFIED_NOT_APPLIED = "CERTIFIED_NOT_APPLIED"
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True, slots=True)
class WorldOutcome:
    """Scripted remote behavior for one attempt (fixture data)."""

    outcome: str  # "success" | "failure" | "timeout"
    remote_id: str | None = None


class FakeWorld:
    """Durable fake remote: applies effect bytes and keeps visibility
    evidence that survives simulated process loss (handoff §7)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(WORLD_SCHEMA_DDL)
        self._scripted: dict[str, WorldOutcome] = {}

    def script(self, attempt_id: str, outcome: WorldOutcome) -> None:
        self._scripted[attempt_id] = outcome

    def apply(
        self,
        *,
        attempt_id: str,
        effect_id: str,
        payload_bytes: bytes,
        now_ms: int,
    ) -> WorldOutcome:
        """Apply exact bytes durably; a timeout still records the attempt
        because the remote may have completed before the response was lost."""
        outcome = self._scripted.get(attempt_id, WorldOutcome("success"))
        remote_id = outcome.remote_id or f"remote:{uuid.uuid5(uuid.NAMESPACE_URL, attempt_id)}"
        with self._conn:
            self._conn.execute(
                "INSERT INTO world_effects (attempt_id, effect_id, payload_bytes,"
                " visible_at_ms, remote_id, outcome) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(attempt_id) DO NOTHING",
                (
                    attempt_id,
                    effect_id,
                    payload_bytes.decode("utf-8"),
                    now_ms,
                    remote_id,
                    outcome.outcome,
                ),
            )
        return outcome

    def visible_bytes(self, attempt_id: str) -> bytes | None:
        row = self._conn.execute(
            "SELECT payload_bytes FROM world_effects WHERE attempt_id = ? AND outcome != 'failure'",
            (attempt_id,),
        ).fetchone()
        return None if row is None else row["payload_bytes"].encode("utf-8")

    def reconcile(self, attempt_id: str) -> str:
        """APPLIED / CERTIFIED_NOT_APPLIED / INDETERMINATE (handoff §6.2).

        Absence from the list is NOT proof of no effect; only a recorded
        failure outcome certifies non-application.
        """
        row = self._conn.execute(
            "SELECT outcome FROM world_effects WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            return ReconcileResult.INDETERMINATE
        if row["outcome"] == "failure":
            return ReconcileResult.CERTIFIED_NOT_APPLIED
        return ReconcileResult.APPLIED

    def close(self) -> None:
        self._conn.close()


class FakeSidecar:
    """Transport-side dispatch: outbox, attempts, leases, receipts.

    Dispatch linearization: durable DISPATCH_STARTED is the local
    linearization point; the world call happens after it and outside any
    core transaction (handoff §5). Crashes at the named checkpoints roll
    back to conservative states; UNKNOWN is resolved only through positive
    reconciliation evidence.
    """

    def __init__(
        self,
        core: CoreStore,
        transport: TransportStore,
        world: FakeWorld,
        clock: VirtualClock,
        injector: FaultInjector | None = None,
        *,
        dispatch_lease_ms: int = DISPATCH_LEASE_MS,
    ) -> None:
        self.core = core
        self.transport = transport
        self.world = world
        self.clock = clock
        self.injector = injector
        self.dispatch_lease_ms = dispatch_lease_ms

    def _trip(self, checkpoint: CrashCheckpoint) -> None:
        if self.injector is not None:
            self.injector.trip(checkpoint)

    def submit_authorized(
        self,
        authorization: CommitAuthorization,
        effect: Effect,
        *,
        rendered_payload: dict[str, Any],
    ) -> DeliveryReceipt:
        """Idempotent dispatch of one authorized effect; exact bytes only."""
        payload_bytes = canonical_json_bytes(rendered_payload)
        auth_ref = record_ref(authorization)
        if authorization.payload_digest != effect.payload_digest:
            raise SidecarError(
                "authorization payload digest does not match the effect",
                ReasonCode.CONFLICT,
            )
        existing_state = self.transport.outbox_state(effect.logical_effect_id)
        receipt = self._existing_receipt(effect.logical_effect_id)
        if existing_state == "SENT" and receipt is not None:
            return receipt  # exact duplicate submit returns current state

        generation = self.transport.transport_generation
        self.transport.enqueue_effect(
            logical_effect_id=effect.logical_effect_id,
            proposal_revision=authorization.proposal_revision,
            proposal_digest=authorization.proposal_digest,
            payload_ref=effect.rendered_payload_ref,
            auth_ref=json.dumps({"id": auth_ref.id, "revision": auth_ref.revision}, sort_keys=True),
            generation=generation,
        )

        attempt_id = self._next_attempt_id(effect.logical_effect_id)
        # PREPARED attempt persisted before its lease is requested (§5).
        self.transport.append_attempt(
            attempt_id=attempt_id,
            effect_id=effect.logical_effect_id,
            state="PREPARED",
            started_at_ms=self.clock.now_ms(),
        )
        now_ms = self.clock.now_ms()
        lease_id = f"lease:{attempt_id}"
        with self.core.transaction() as connection:
            connection.execute(
                "INSERT INTO core_dispatch_leases (lease_id, attempt_id, effect_id, auth_ref,"
                " payload_digest, runtime_epoch, transport_generation, expires_at_ms, state)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ISSUED')",
                (
                    lease_id,
                    attempt_id,
                    effect.logical_effect_id,
                    json.dumps({"id": auth_ref.id, "revision": auth_ref.revision}, sort_keys=True),
                    authorization.payload_digest,
                    self.core.runtime_epoch,
                    generation,
                    now_ms + self.dispatch_lease_ms,
                ),
            )
        self._trip(CrashCheckpoint.AFTER_RESERVE_BEFORE_LEASE_RETURN)

        # Single-use consumption with epoch/generation/expiry fencing.
        with self.core.transaction() as connection:
            row = connection.execute(
                "SELECT state, runtime_epoch, transport_generation, expires_at_ms"
                " FROM core_dispatch_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
            if row is None or row["state"] != "ISSUED":
                raise SidecarError("lease not issuable", ReasonCode.LEASE_CONSUMED)
            if row["runtime_epoch"] != self.core.runtime_epoch:
                raise SidecarError("stale core epoch", ReasonCode.STALE_EPOCH)
            if row["transport_generation"] != generation:
                raise SidecarError("stale transport generation", ReasonCode.STALE_GENERATION)
            if row["expires_at_ms"] < self.clock.now_ms():
                raise SidecarError("lease expired", ReasonCode.LEASE_EXPIRED)
            connection.execute(
                "UPDATE core_dispatch_leases SET state = 'CONSUMED' WHERE lease_id = ?"
                " AND state = 'ISSUED'",
                (lease_id,),
            )
        self._trip(CrashCheckpoint.AFTER_LEASE_CONSUME_BEFORE_DISPATCH_START)

        # Durable DISPATCH_STARTED: the local linearization point.
        self.transport.transition_outbox(effect.logical_effect_id, "DISPATCH_STARTED")
        self._trip(CrashCheckpoint.AFTER_DISPATCH_START_BEFORE_WORLD)

        outcome = self.world.apply(
            attempt_id=attempt_id,
            effect_id=effect.logical_effect_id,
            payload_bytes=payload_bytes,
            now_ms=self.clock.now_ms(),
        )
        self._trip(CrashCheckpoint.AFTER_WORLD_BEFORE_RECEIPT)

        if outcome.outcome == "success":
            state = DeliveryState.SENT
            self.transport.transition_outbox(effect.logical_effect_id, "SENT")
            error_code = None
        elif outcome.outcome == "failure":
            state = DeliveryState.FAILED_CONFIRMED
            self.transport.transition_outbox(effect.logical_effect_id, "FAILED_CONFIRMED")
            error_code = ReasonCode.CONFIRMED_NO_EFFECT
        else:  # timeout: conservative UNKNOWN, resolved only by reconciliation
            state = DeliveryState.UNKNOWN
            self.transport.transition_outbox(effect.logical_effect_id, "UNKNOWN")
            error_code = ReasonCode.DELIVERY_UNKNOWN

        receipt = DeliveryReceipt(
            id=f"receipt:{attempt_id}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            effect_id=effect.logical_effect_id,
            proposal_revision=authorization.proposal_revision,
            attempt_id=attempt_id,
            state=state,
            fake_remote_id=outcome.remote_id,
            generation=generation,
            error_code=error_code,
            observed_at=self.clock.now_rfc3339(),
        )
        self.core.put_record(receipt)
        return receipt

    def reconcile(self, effect_id: str) -> str:
        """Status/reconcile via world evidence; never a blind retry."""
        state = self.transport.outbox_state(effect_id)
        if state == "UNKNOWN":
            row = self.transport._conn.execute(
                "SELECT attempt_id FROM transport_attempts WHERE effect_id = ?"
                " ORDER BY started_at_ms DESC LIMIT 1",
                (effect_id,),
            ).fetchone()
            if row is None:
                return ReconcileResult.INDETERMINATE
            verdict = self.world.reconcile(row["attempt_id"])
            if verdict == ReconcileResult.APPLIED:
                self.transport.transition_outbox(effect_id, "SENT")
            elif verdict == ReconcileResult.CERTIFIED_NOT_APPLIED:
                self.transport.transition_outbox(effect_id, "FAILED_CONFIRMED")
            return verdict
        return state or ReconcileResult.INDETERMINATE

    def cancel_or_supersede(self, effect_id: str) -> str:
        """Durable pre-dispatch cancellation; never pretends to stop in-flight."""
        self._trip(CrashCheckpoint.DURING_CANCEL)
        acknowledged = self.transport.acknowledge_supersession(effect_id)
        return "CANCELLED" if acknowledged else "TOO_LATE"

    def _next_attempt_id(self, effect_id: str) -> str:
        count = self.transport._conn.execute(
            "SELECT count(*) FROM transport_attempts WHERE effect_id = ?", (effect_id,)
        ).fetchone()[0]
        return f"attempt:{effect_id}:{count + 1}"

    def _existing_receipt(self, effect_id: str) -> DeliveryReceipt | None:
        attempt = self.transport._conn.execute(
            "SELECT attempt_id FROM transport_attempts WHERE effect_id = ?"
            " ORDER BY started_at_ms DESC LIMIT 1",
            (effect_id,),
        ).fetchone()
        if attempt is None:
            return None
        record = self.core.get_record(f"receipt:{attempt['attempt_id']}")
        assert record is None or isinstance(record, DeliveryReceipt)
        return record


def record_ref(record: Record) -> RecordRef:
    return RecordRef(id=record.id, revision=record.revision)
