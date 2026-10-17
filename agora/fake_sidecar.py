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
from typing import Any, ClassVar

from agora.contracts import (
    AudienceSnapshot,
    AuthorizationStatus,
    CapabilityEnvelope,
    CommitAuthorization,
    ContextSnapshot,
    DeliveryReceipt,
    DeliveryState,
    Effect,
    ReasonCode,
    Record,
    RecordRef,
    RecordStatus,
    SnapshotStatus,
    canonical_json_bytes,
)
from agora.ports import CrashCheckpoint, FaultInjector, VirtualClock
from agora.stores import CoreStore, RecordConflictError, TransportStore

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

    RATE_WINDOW_MS: ClassVar[dict[str, int]] = {"per_minute": 60_000, "per_hour": 3_600_000}

    def submit_authorized(
        self,
        authorization: CommitAuthorization,
        effect: Effect,
        *,
        rendered_payload: dict[str, Any],
        snapshot_ref: RecordRef | None = None,
    ) -> DeliveryReceipt:
        """Idempotent dispatch of one authorized effect; exact bytes only.

        Revalidation happens inside the fenced lease-consume transaction,
        before the durable DISPATCH_STARTED (M3 D1, I08): the
        authorization, its consulted policy vector, the bound snapshot, a
        reaction's target, the audience's material shape, the capability,
        and any rate allowance are re-checked in that one serialized
        window. A mismatch transitions the outbox row to HELD with the
        typed code and dispatches nothing; a change landing after
        DISPATCH_STARTED is too late for prevention and resolves
        conservatively through UNKNOWN semantics."""
        payload_bytes = canonical_json_bytes(rendered_payload)
        auth_ref = record_ref(authorization)
        if authorization.payload_digest != effect.payload_digest:
            raise SidecarError(
                "authorization payload digest does not match the effect",
                ReasonCode.CONFLICT,
            )
        stored_payload = self.core.get_payload(effect.rendered_payload_ref)
        if stored_payload is None or stored_payload.encode("utf-8") != payload_bytes:
            # exact bytes only: the world receives the effect's durable
            # rendering, never caller-supplied bytes that differ from it
            raise SidecarError(
                "rendered payload does not match the effect's durable bytes",
                ReasonCode.CONFLICT,
            )
        existing_state = self.transport.outbox_state(effect.logical_effect_id)
        # I09/D2 first: a different proposal revision for the same logical
        # effect requeues only through acknowledged cancellation; TOO_LATE
        # (the prior revision already started dispatching, or already
        # delivered) refuses the requeue and the earlier revision's
        # outcome stands.
        if existing_state is not None:
            row = self.transport._conn.execute(
                "SELECT proposal_revision FROM transport_outbox WHERE logical_effect_id = ?",
                (effect.logical_effect_id,),
            ).fetchone()
            if row is not None and row["proposal_revision"] != authorization.proposal_revision:
                if row["proposal_revision"] > authorization.proposal_revision:
                    raise SidecarError(
                        "a newer proposal revision already owns this logical effect",
                        ReasonCode.CONFLICT,
                    )
                if self.cancel_or_supersede(effect.logical_effect_id) != "CANCELLED":
                    raise SidecarError(
                        "prior revision is in flight; requeue refused", ReasonCode.TOO_LATE
                    )
            elif row is not None and existing_state == "CANCELLED":
                # an acknowledged cancellation retires that revision; only a
                # newer revision may requeue the logical effect
                raise SidecarError(
                    "cancelled revision cannot be resubmitted; submit a newer revision",
                    ReasonCode.CONFLICT,
                )
        receipt = self._existing_receipt(effect.logical_effect_id)
        if existing_state == "SENT" and receipt is not None:
            return receipt  # exact duplicate submit returns current state
        existing_state = self.transport.outbox_state(effect.logical_effect_id)
        if existing_state == "HELD":
            # a held effect re-enters through the queue: the rewrite loop's
            # replacement (or a later window's retry) dispatches from QUEUED
            self.transport.transition_outbox(effect.logical_effect_id, "QUEUED")

        generation = self.transport.transport_generation
        preconditions = (
            (
                json.dumps(
                    {"id": snapshot_ref.id, "revision": snapshot_ref.revision}, sort_keys=True
                ),
            )
            if snapshot_ref is not None
            else ()
        )
        try:
            self.transport.enqueue_effect(
                logical_effect_id=effect.logical_effect_id,
                proposal_revision=authorization.proposal_revision,
                proposal_digest=authorization.proposal_digest,
                payload_ref=effect.rendered_payload_ref,
                auth_ref=json.dumps(
                    {"id": auth_ref.id, "revision": auth_ref.revision}, sort_keys=True
                ),
                generation=generation,
                target_preconditions=preconditions,
            )
        except RecordConflictError as error:
            # same logical id and revision with different bytes: the M1
            # structural refusal, surfaced as the spec's typed CONFLICT
            raise SidecarError(str(error), ReasonCode.CONFLICT) from error
        return self._dispatch_attempt(
            authorization=authorization,
            effect=effect,
            payload_bytes=payload_bytes,
            generation=generation,
            snapshot_ref=snapshot_ref,
        )

    def _dispatch_attempt(
        self,
        *,
        authorization: CommitAuthorization,
        effect: Effect,
        payload_bytes: bytes,
        generation: int,
        snapshot_ref: RecordRef | None,
    ) -> DeliveryReceipt:
        auth_ref = record_ref(authorization)
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

        # Single-use consumption with epoch/generation/expiry fencing. The
        # same serialized window revalidates every bound version and
        # reserves any rate allowance (I08/I15) — prevention lives here,
        # never after the linearization point.
        hold_code: ReasonCode | None = None
        with self.core.transaction() as connection:
            self._fence_consume_lease_tx(connection, lease_id, generation)
            hold_code = self._revalidation_hold(
                connection,
                authorization=authorization,
                effect=effect,
                snapshot_ref=snapshot_ref,
            )
            if hold_code is not None:
                # the lease is spent on the hold, not on a dispatch
                connection.execute(
                    "UPDATE core_dispatch_leases SET state = 'CANCELLED' WHERE lease_id = ?",
                    (lease_id,),
                )
            else:
                connection.execute(
                    "UPDATE core_dispatch_leases SET state = 'CONSUMED' WHERE lease_id = ?"
                    " AND state = 'ISSUED'",
                    (lease_id,),
                )
        if hold_code is not None:
            self.transport.transition_outbox(effect.logical_effect_id, "HELD")
            raise SidecarError(f"dispatch held: {hold_code.value}", hold_code)
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
            self.core.settle_release(effect.logical_effect_id, "COMMITTED")
        elif outcome.outcome == "failure":
            state = DeliveryState.FAILED_CONFIRMED
            self.transport.transition_outbox(effect.logical_effect_id, "FAILED_CONFIRMED")
            error_code = ReasonCode.CONFIRMED_NO_EFFECT
            # the allowance stays spent: the attempt consumed the window
            # slot and the single retry rides the same reservation
        else:  # timeout: conservative UNKNOWN, resolved only by reconciliation
            state = DeliveryState.UNKNOWN
            self.transport.transition_outbox(effect.logical_effect_id, "UNKNOWN")
            error_code = ReasonCode.DELIVERY_UNKNOWN
            self.core.settle_release(effect.logical_effect_id, "POSSIBLY_COMMITTED")

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

    def _fence_consume_lease_tx(
        self, connection: sqlite3.Connection, lease_id: str, generation: int
    ) -> None:
        """Single-use lease consumption with epoch/generation/expiry
        fencing (I10): one sidecar generation executes, and a stale epoch,
        a stale generation, an expired or an already-consumed lease can
        never start dispatch. Raises the typed SidecarError inside the
        caller's transaction, so nothing partial commits."""
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

    def _revalidation_hold(
        self,
        connection: sqlite3.Connection,
        *,
        authorization: CommitAuthorization,
        effect: Effect,
        snapshot_ref: RecordRef | None,
    ) -> ReasonCode | None:
        """D1/I08: re-read every bound version inside the serialized
        window. Returns the typed hold code, or None when dispatch may
        proceed. Reads go through the same connection, so they see exactly
        the serialized state."""
        current_auth = self.core.get_record(record_ref(authorization).id)
        if (
            not isinstance(current_auth, CommitAuthorization)
            or current_auth.revision != authorization.revision
            or current_auth.status != AuthorizationStatus.ACTIVE
        ):
            return ReasonCode.STALE_CONTEXT
        for ref in authorization.policy_version_vector:
            record = self.core.get_record(ref.id)
            if record is None or record.revision != ref.revision:
                return ReasonCode.POLICY_CHANGED
        snapshot: ContextSnapshot | None = None
        if snapshot_ref is not None:
            pinned = self.core.get_record(snapshot_ref.id, snapshot_ref.revision)
            current = self.core.current_ref(snapshot_ref.id)
            if (
                not isinstance(pinned, ContextSnapshot)
                or pinned.status != SnapshotStatus.ACTIVE
                or current is None
                or current.revision != snapshot_ref.revision
            ):
                return ReasonCode.STALE_CONTEXT
            snapshot = pinned
        if snapshot is not None:
            # D3: material audience change is exactly a disclosure-context
            # revision move or a visibility/unknown-readers flip; message
            # growth and same-class history churn are NOT material.
            bound_audience = self.core.get_record(
                snapshot.audience_ref.id, snapshot.audience_ref.revision
            )
            current_audience = self.core.get_record(snapshot.audience_ref.id)
            if (
                isinstance(bound_audience, AudienceSnapshot)
                and isinstance(current_audience, AudienceSnapshot)
                and (
                    current_audience.disclosure_context_revision
                    != authorization.audience_disclosure_revision
                    or (current_audience.visibility, current_audience.unknown_readers)
                    != (bound_audience.visibility, bound_audience.unknown_readers)
                )
            ):
                return ReasonCode.AUDIENCE_CHANGED
        if effect.target_ref is not None:
            current_target = self.core.current_ref(effect.target_ref.id)
            if current_target is None or current_target.revision != effect.target_ref.revision:
                return ReasonCode.TARGET_CHANGED
        capability = self.core.get_record(authorization.capability_ref.id)
        if (
            not isinstance(capability, CapabilityEnvelope)
            or capability.revision != authorization.capability_ref.revision
            or capability.status != RecordStatus.ACTIVE
        ):
            return ReasonCode.CAPABILITY_INVALID
        return self._reserve_release(connection, effect=effect, capability=capability)

    def _reserve_release(
        self,
        connection: sqlite3.Connection,
        *,
        effect: Effect,
        capability: CapabilityEnvelope,
    ) -> ReasonCode | None:
        """I15/D4: spend the fixture-seeded rate allowance on the M1
        reservation ledger — one row per effect, atomic SUM under the
        already-open BEGIN IMMEDIATE window, window encoded in the query
        key. Unreservable effects hold with RELEASE_LIMIT for a later
        window."""
        for limit_pair in capability.rate_budget_limits:
            name = limit_pair.first
            window_ms = self.RATE_WINDOW_MS.get(name)
            if window_ms is None:
                # a fixture-seeded limit kind the runtime cannot enforce is
                # a registry mismatch: fail closed with the typed code,
                # never a silently unenforced budget (review finding)
                return ReasonCode.UNSUPPORTED
            limit = limit_pair.second
            if not isinstance(limit, int):
                return ReasonCode.UNSUPPORTED
            now_ms = self.clock.now_ms()
            window_start = now_ms - (now_ms % window_ms)
            query_key = f"{effect.account_id}:{effect.destination_id}:{name}:{window_start}"
            reserved = self.core._reserve_release_tx(
                connection,
                effect_id=effect.logical_effect_id,
                constraint_group_id=f"{capability.id}:{name}",
                query_key=query_key,
                units=1,
                limit=limit,
            )
            if not reserved:
                return ReasonCode.RELEASE_LIMIT
        return None

    def reconcile(self, effect_id: str) -> str:
        """Status/reconcile via world evidence; never a blind retry.

        UNKNOWN and a restart-recovered DISPATCH_STARTED are both resolved
        only through positive world evidence (§6.2): APPLIED → SENT,
        CERTIFIED_NOT_APPLIED → FAILED_CONFIRMED, and INDETERMINATE marks
        the conservative UNKNOWN state rather than guessing no-send."""
        state = self.transport.outbox_state(effect_id)
        if state in ("UNKNOWN", "DISPATCH_STARTED"):
            row = self.transport._conn.execute(
                "SELECT attempt_id FROM transport_attempts WHERE effect_id = ?"
                " ORDER BY started_at_ms DESC, rowid DESC LIMIT 1",
                (effect_id,),
            ).fetchone()
            if row is None:
                return ReconcileResult.INDETERMINATE
            verdict = self.world.reconcile(row["attempt_id"])
            if verdict == ReconcileResult.APPLIED:
                self.transport.transition_outbox(effect_id, "SENT")
            elif verdict == ReconcileResult.CERTIFIED_NOT_APPLIED:
                self.transport.transition_outbox(effect_id, "FAILED_CONFIRMED")
            elif state == "DISPATCH_STARTED":
                # conservative recovery: the world cannot answer, so the
                # in-flight effect becomes UNKNOWN rather than assumed sent
                self.transport.transition_outbox(effect_id, "UNKNOWN")
            return verdict
        return state or ReconcileResult.INDETERMINATE

    def retry_failed(
        self,
        authorization: CommitAuthorization,
        effect: Effect,
        *,
        rendered_payload: dict[str, Any],
        snapshot_ref: RecordRef | None = None,
    ) -> DeliveryReceipt:
        """I11/§6.2: FAILED_CONFIRMED retries exactly once — only with
        fake-world evidence of no effect for the failed attempt, a fresh
        attempt id with the logical effect id preserved, and still-valid
        dependencies revalidated in the same fenced window as a first
        dispatch. UNKNOWN never retries; only positive world evidence
        resolves it."""
        if authorization.payload_digest != effect.payload_digest:
            raise SidecarError(
                "authorization payload digest does not match the effect",
                ReasonCode.CONFLICT,
            )
        state = self.transport.outbox_state(effect.logical_effect_id)
        if state != "FAILED_CONFIRMED":
            raise SidecarError("only FAILED_CONFIRMED may retry", ReasonCode.CONFLICT)
        count = self.transport._conn.execute(
            "SELECT count(*) FROM transport_attempts WHERE effect_id = ?",
            (effect.logical_effect_id,),
        ).fetchone()[0]
        if count >= 2:
            raise SidecarError("retry budget exhausted", ReasonCode.TOO_LATE)
        last_attempt = self.transport._conn.execute(
            "SELECT attempt_id FROM transport_attempts WHERE effect_id = ?"
            " ORDER BY started_at_ms DESC, rowid DESC LIMIT 1",
            (effect.logical_effect_id,),
        ).fetchone()
        if (
            last_attempt is None
            or self.world.reconcile(last_attempt["attempt_id"])
            != ReconcileResult.CERTIFIED_NOT_APPLIED
        ):
            raise SidecarError("retry requires certified no-effect evidence", ReasonCode.CONFLICT)
        self.transport.transition_outbox(effect.logical_effect_id, "QUEUED")
        return self._dispatch_attempt(
            authorization=authorization,
            effect=effect,
            payload_bytes=canonical_json_bytes(rendered_payload),
            generation=self.transport.transport_generation,
            snapshot_ref=snapshot_ref,
        )

    def cancel_or_supersede(self, effect_id: str) -> str:
        """Durable pre-dispatch cancellation; never pretends to stop
        in-flight. An acknowledged cancellation is also the only receipt
        that releases the effect's unused reservation (addendum §5) — but
        only for a row that never reached DISPATCH_STARTED: a
        FAILED_CONFIRMED revision was dispatched and its allowance stays
        spent (review finding: cancelling it must not over-admit the
        window or mislabel the ledger as CANCELLED_BEFORE_DISPATCH)."""
        state = self.transport.outbox_state(effect_id)
        self._trip(CrashCheckpoint.DURING_CANCEL)
        acknowledged = self.transport.acknowledge_supersession(effect_id)
        if acknowledged and state in ("QUEUED", "HELD"):
            self.core.settle_release(effect_id, "CANCELLED_BEFORE_DISPATCH")
        return "CANCELLED" if acknowledged else "TOO_LATE"

    def _next_attempt_id(self, effect_id: str) -> str:
        count = self.transport._conn.execute(
            "SELECT count(*) FROM transport_attempts WHERE effect_id = ?", (effect_id,)
        ).fetchone()[0]
        return f"attempt:{effect_id}:{count + 1}"

    def _existing_receipt(self, effect_id: str) -> DeliveryReceipt | None:
        attempt = self.transport._conn.execute(
            "SELECT attempt_id FROM transport_attempts WHERE effect_id = ?"
            " ORDER BY started_at_ms DESC, rowid DESC LIMIT 1",
            (effect_id,),
        ).fetchone()
        if attempt is None:
            return None
        record = self.core.get_record(f"receipt:{attempt['attempt_id']}")
        assert record is None or isinstance(record, DeliveryReceipt)
        return record


def record_ref(record: Record) -> RecordRef:
    return RecordRef(id=record.id, revision=record.revision)
