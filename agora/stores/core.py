"""The core ownership domain (handoff §7): records, payloads, memory
scope, dependencies, ingestion rows, work, release reservations, leases,
audit and meta in ``core.sqlite``."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from agora.contracts import (
    ANY_TARGET,
    REASON_CODES,
    RECORD_TYPES,
    DependencyKind,
    MemoryRecord,
    MemoryStage,
    PayloadStatus,
    Record,
    RecordRef,
    ValidationError,
    WorkState,
    canonical_json_bytes,
    iter_ref_checks,
    record_from_payload,
    record_to_payload,
    strict_json_loads,
)

from ._base import (
    DependencyCycleError,
    InvalidReferenceError,
    RecordConflictError,
    StoreError,
    TransitionError,
    _Domain,
    _tx,
)

CORE_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS core_records (
    record_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    record_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (record_id, revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS core_records_current
    ON core_records (record_id) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS core_payloads (
    payload_id TEXT PRIMARY KEY,
    owner_ref TEXT NOT NULL,
    data TEXT NOT NULL,
    sensitivity_refs TEXT NOT NULL,
    commitment_refs TEXT NOT NULL,
    expires_at_ms INTEGER,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'PURGED'))
);

CREATE TABLE IF NOT EXISTS core_memory_scope (
    memory_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    stage TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    scope_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    expiry_ms INTEGER,
    assertion_ref TEXT NOT NULL,
    commitment_refs TEXT NOT NULL,
    PRIMARY KEY (memory_id, revision)
);

CREATE TABLE IF NOT EXISTS core_dependencies (
    child_ref TEXT NOT NULL,
    parent_ref TEXT NOT NULL,
    dependency_kind TEXT NOT NULL
        CHECK (dependency_kind IN ('PRODUCER_INPUT', 'DECLARED', 'CONTROL')),
    PRIMARY KEY (child_ref, parent_ref, dependency_kind)
);
CREATE INDEX IF NOT EXISTS core_dependencies_parent
    ON core_dependencies (parent_ref);

CREATE TABLE IF NOT EXISTS core_ingestion (
    consumer_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    journal_seq INTEGER NOT NULL,
    fragment_refs TEXT NOT NULL,
    recovery TEXT NOT NULL CHECK (recovery IN ('COMPLETE', 'GAP', 'UNKNOWN')),
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (consumer_id, source_event_id, source_revision),
    UNIQUE (consumer_id, journal_seq)
);

CREATE TABLE IF NOT EXISTS core_work (
    work_id TEXT PRIMARY KEY,
    trigger_key TEXT NOT NULL UNIQUE,
    intent_ref TEXT NOT NULL,
    episode_ref TEXT,
    state TEXT NOT NULL CHECK (state IN ('READY', 'RUNNING', 'HOLD', 'COMPLETED')),
    lease_owner TEXT,
    lease_until_ms INTEGER,
    runtime_epoch INTEGER NOT NULL,
    main_steps_used INTEGER NOT NULL,
    submissions_used INTEGER NOT NULL,
    terminal_outcome_ref TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS core_work_episode_lease
    ON core_work (episode_ref) WHERE lease_owner IS NOT NULL;

CREATE TABLE IF NOT EXISTS core_release_reservations (
    reservation_id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL,
    constraint_group_id TEXT NOT NULL,
    query_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('RESERVED', 'COMMITTED', 'POSSIBLY_COMMITTED', 'CANCELLED_BEFORE_DISPATCH')
    ),
    accounting_units INTEGER NOT NULL,
    UNIQUE (effect_id, constraint_group_id)
);

CREATE TABLE IF NOT EXISTS core_dispatch_leases (
    lease_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    effect_id TEXT NOT NULL,
    auth_ref TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    runtime_epoch INTEGER NOT NULL,
    transport_generation INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ISSUED', 'CONSUMED', 'EXPIRED', 'CANCELLED'))
);

CREATE TABLE IF NOT EXISTS core_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT NOT NULL,
    entity_ref TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    version_refs TEXT NOT NULL,
    at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS core_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class CoreStore(_Domain):
    """Core ownership domain: records, payloads, memory scope, dependencies,
    ingestion, work, reservations, leases, audit, meta."""

    _meta_table = "core_meta"

    def __init__(self, path: Path | str) -> None:
        super().__init__(Path(path), CORE_SCHEMA_DDL)
        if self._fresh:
            self._set_meta_tx("runtime_epoch", 0)
            self._set_meta_tx("cursor", 0)
        else:
            # Restart increments the durable monotonic epoch (handoff §3) and
            # recovers §6.1 work state: a lease lost during restart returns
            # the work to READY with budgets and dedup key retained.
            epoch = int(self.get_meta("runtime_epoch") or 0)
            with _tx(self._conn):
                self._set_meta_tx("runtime_epoch", epoch + 1)
                self._conn.execute(
                    "UPDATE core_work SET state = 'READY', lease_owner = NULL,"
                    " lease_until_ms = NULL WHERE state = 'RUNNING'"
                )

    @property
    def runtime_epoch(self) -> int:
        return int(self.get_meta("runtime_epoch") or 0)

    @property
    def cursor(self) -> int:
        return int(self.get_meta("cursor") or 0)

    # -- versioned records --------------------------------------------------

    def put_record(self, record: Record, *, text_field_refs: dict[str, str] | None = None) -> None:
        """Append a new revision and flip the current pointer in one
        transaction. Existing (id, revision) rows are never edited: a
        duplicate insert raises RecordConflictError.

        ``text_field_refs`` maps a bounded text field to the record's own
        payload-ref field (design D5): the text is swapped into the
        out-of-line payload store and the stored record JSON carries only
        the ref, never the bytes.

        Typed refs (handoff §7): every RecordRef field is verified against
        the REF_TARGETS map inside the same transaction — a missing target
        or a wrong target type raises InvalidReferenceError with nothing
        written. Targets must already exist; use ``put_records`` when a
        batch needs intra-batch forward references.
        """
        with _tx(self._conn) as connection:
            self._put_record_tx(connection, record, text_field_refs=text_field_refs)
            self._verify_ref_targets_tx(connection, (record,))

    def put_records(
        self,
        records: tuple[Record, ...] | list[Record],
        *,
        text_field_refs: dict[str, dict[str, str]] | None = None,
    ) -> None:
        """Insert several records in ONE transaction, verifying references
        only after every insert: intra-batch forward and circular references
        resolve (fixture loads; view/certificate pairs). Any failure —
        conflict or invalid reference — rolls back the whole batch.
        ``text_field_refs`` is keyed by record id, each value the same per-
        record mapping ``put_record`` takes.
        """
        batch = tuple(records)
        swaps = text_field_refs or {}
        with _tx(self._conn) as connection:
            for record in batch:
                self._put_record_tx(connection, record, text_field_refs=swaps.get(record.id))
            self._verify_ref_targets_tx(connection, batch)

    def promote_memory(self, memory: MemoryRecord, *, expiry_ms: int | None) -> None:
        """Persist one memory assertion revision and its scope-index row in
        ONE transaction (§7 core.memory_scope: eligibility is filtered on
        the index before any candidate payload is read). ``expiry_ms`` is
        virtual milliseconds; the caller (who owns the clock) converts the
        record's ``valid_until``."""
        with _tx(self._conn) as connection:
            self._put_record_tx(connection, memory)
            self._verify_ref_targets_tx(connection, (memory,))
            connection.execute(
                "INSERT INTO core_memory_scope (memory_id, revision, stage, scope_kind,"
                " scope_ref, status, expiry_ms, assertion_ref, commitment_refs)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (memory_id, revision) DO NOTHING",
                (
                    memory.id,
                    memory.revision,
                    memory.stage.value,
                    memory.scope_kind.value,
                    memory.scope_ref,
                    memory.status.value,
                    expiry_ms,
                    memory.assertion_fragment_ref.id,
                    json.dumps([f"{ref.id}:{ref.revision}" for ref in memory.commitment_refs]),
                ),
            )

    def memory_scope_rows(self, memory_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM core_memory_scope WHERE memory_id = ? ORDER BY revision",
            (memory_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def eligible_memory_refs(
        self,
        *,
        scope_kind: str,
        scope_ref: str,
        now_ms: int,
        stages: frozenset[MemoryStage] = frozenset(MemoryStage),
    ) -> tuple[RecordRef, ...]:
        """Index-only eligibility (§7): stage/scope/status/expiry filtering
        happens on core_memory_scope before any candidate payload is read.
        AGENT-scoped memories are the agent's own and stay eligible in every
        context (cross-room recall); other scopes match only their own ref —
        episode membership or room merges confer no scope rights. Rows for
        superseded revisions are ignored (the current revision wins)."""
        if not stages:
            return ()
        placeholders = ",".join("?" for _ in stages)
        rows = self._conn.execute(
            "SELECT ms.memory_id, ms.revision FROM core_memory_scope ms"
            " JOIN core_records r ON r.record_id = ms.memory_id AND r.revision = ms.revision"
            " AND r.is_current = 1"
            " WHERE ms.status = 'ACTIVE'"
            " AND (ms.scope_kind = 'AGENT' OR (ms.scope_kind = ? AND ms.scope_ref = ?))"
            " AND (ms.expiry_ms IS NULL OR ms.expiry_ms > ?)"
            f" AND ms.stage IN ({placeholders})"
            " ORDER BY ms.memory_id",
            (scope_kind, scope_ref, now_ms, *(stage.value for stage in stages)),
        ).fetchall()
        return tuple(RecordRef(id=row["memory_id"], revision=row["revision"]) for row in rows)

    def verify_reference(
        self, ref: RecordRef, allowed: str | frozenset[str], *, label: str
    ) -> None:
        """Resolve one reference outside an insert (coordinator
        pre-validation of model-supplied ids, handoff §5: "the coordinator
        ... validates referenced IDs")."""
        row = self._conn.execute(
            "SELECT record_type FROM core_records WHERE record_id = ? AND revision = ?",
            (ref.id, ref.revision),
        ).fetchone()
        if row is None:
            raise InvalidReferenceError(label, ref, "does not exist")
        expected = {allowed} if isinstance(allowed, str) else set(allowed)
        if row["record_type"] not in expected:
            raise InvalidReferenceError(
                label, ref, f"is a {row['record_type']}, expected {sorted(expected)}"
            )

    def _put_record_tx(
        self,
        connection: sqlite3.Connection,
        record: Record,
        *,
        text_field_refs: dict[str, str] | None = None,
    ) -> None:
        payload = record_to_payload(record)
        owner = json.dumps({"id": record.id, "revision": record.revision}, sort_keys=True)
        for field_name, ref_field_name in (text_field_refs or {}).items():
            text = payload.get(field_name)
            if not isinstance(text, str) or not text:
                raise ValidationError(
                    f"text field {field_name} must be a non-empty string",
                    path=field_name,
                )
            payload_id = f"payload:{record.TYPE_NAME}:{record.id}:{record.revision}:{field_name}"
            # Idempotent under crash replay: a re-swapped identical record
            # re-inserts the same payload id; differing text under the same
            # (id, revision) fails the record conflict check below anyway.
            connection.execute(
                "INSERT INTO core_payloads (payload_id, owner_ref, data, sensitivity_refs,"
                " commitment_refs, expires_at_ms, status) VALUES (?, ?, ?, ?, ?, NULL, ?)"
                " ON CONFLICT(payload_id) DO NOTHING",
                (payload_id, owner, text, "[]", "[]", PayloadStatus.ACTIVE.value),
            )
            payload[field_name] = None
            payload[ref_field_name] = payload_id
        record_epoch = getattr(record, "runtime_epoch", None)
        if record_epoch is not None and record_epoch != self.runtime_epoch:
            raise TransitionError(
                f"record {record.id} carries epoch {record_epoch},"
                f" store is at {self.runtime_epoch}: STALE_EPOCH",
            )
        connection.execute(
            "UPDATE core_records SET is_current = 0 WHERE record_id = ? AND is_current = 1"
            " AND revision < ?",
            (record.id, record.revision),
        )
        payload_text = canonical_json_bytes(payload).decode("utf-8")
        try:
            connection.execute(
                "INSERT INTO core_records (record_id, revision, record_type, payload_json,"
                " is_current, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                (
                    record.id,
                    record.revision,
                    record.TYPE_NAME,
                    payload_text,
                    record.created_at,
                ),
            )
        except sqlite3.IntegrityError:
            # History is never edited — but a crash-replayed step
            # regenerates byte-identical records, and re-accepting the
            # same fact is idempotent (§7 replay dedup). Identical bytes
            # pass silently; different bytes are a CONFLICT.
            existing = connection.execute(
                "SELECT payload_json FROM core_records WHERE record_id = ? AND revision = ?",
                (record.id, record.revision),
            ).fetchone()
            if existing is None or existing["payload_json"] != payload_text:
                raise RecordConflictError(
                    f"record {record.id} revision {record.revision} already exists"
                    " with different bytes"
                ) from None

    def _verify_ref_targets_tx(
        self, connection: sqlite3.Connection, records: tuple[Record, ...]
    ) -> None:
        """Check every typed-ref field of ``records`` against rows visible
        in this transaction (committed or inserted earlier in it)."""
        checks = [
            (f"{record.TYPE_NAME}.{field}", ref, allowed)
            for record in records
            for field, ref, allowed in iter_ref_checks(record)
        ]
        if not checks:
            return
        pairs = {(ref.id, ref.revision) for _, ref, _ in checks}
        placeholders = ",".join("(?,?)" for _ in pairs)
        flat = [value for pair in pairs for value in pair]
        rows = connection.execute(
            "SELECT record_id, revision, record_type FROM core_records"
            f" WHERE (record_id, revision) IN (VALUES {placeholders})",
            flat,
        ).fetchall()
        resolved = {(row["record_id"], row["revision"]): row["record_type"] for row in rows}
        for label, ref, allowed in checks:
            found = resolved.get((ref.id, ref.revision))
            if found is None:
                raise InvalidReferenceError(label, ref, "does not exist")
            if allowed == ANY_TARGET:
                continue
            expected = {allowed} if isinstance(allowed, str) else set(allowed)
            if found not in expected:
                raise InvalidReferenceError(
                    label, ref, f"is a {found}, expected {sorted(expected)}"
                )

    def get_record(self, record_id: str, revision: int | None = None) -> Record | None:
        if revision is None:
            row = self._conn.execute(
                "SELECT * FROM core_records WHERE record_id = ? AND is_current = 1", (record_id,)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM core_records WHERE record_id = ? AND revision = ?",
                (record_id, revision),
            ).fetchone()
        if row is None:
            return None
        record_cls = RECORD_TYPES.get(row["record_type"])
        if record_cls is None:
            raise StoreError(f"unknown record type {row['record_type']!r} in storage")
        return record_from_payload(record_cls, strict_json_loads(row["payload_json"]))

    def current_ref(self, record_id: str) -> RecordRef | None:
        row = self._conn.execute(
            "SELECT revision FROM core_records WHERE record_id = ? AND is_current = 1",
            (record_id,),
        ).fetchone()
        return None if row is None else RecordRef(id=record_id, revision=row["revision"])

    def record_revisions(self, record_id: str) -> tuple[int, ...]:
        rows = self._conn.execute(
            "SELECT revision FROM core_records WHERE record_id = ? ORDER BY revision",
            (record_id,),
        ).fetchall()
        return tuple(row["revision"] for row in rows)

    # -- out-of-line payloads -----------------------------------------------

    def put_payload(
        self,
        payload_id: str,
        data: str,
        *,
        owner_ref: str,
        sensitivity_refs: tuple[str, ...] = (),
        commitment_refs: tuple[str, ...] = (),
        expires_at_ms: int | None = None,
    ) -> None:
        with _tx(self._conn):
            self._conn.execute(
                "INSERT INTO core_payloads (payload_id, owner_ref, data, sensitivity_refs,"
                " commitment_refs, expires_at_ms, status) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(payload_id) DO NOTHING",
                (
                    payload_id,
                    owner_ref,
                    data,
                    json.dumps(list(sensitivity_refs)),
                    json.dumps(list(commitment_refs)),
                    expires_at_ms,
                    PayloadStatus.ACTIVE.value,
                ),
            )

    def get_payload(self, payload_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT data, status FROM core_payloads WHERE payload_id = ?", (payload_id,)
        ).fetchone()
        if row is None or row["status"] != PayloadStatus.ACTIVE.value:
            return None
        return row["data"]

    def purge_expired_payloads(self, now_ms: int) -> tuple[str, ...]:
        """Virtual-time expiry: purge bytes, retain metadata refs (§6.4)."""
        with _tx(self._conn):
            rows = self._conn.execute(
                "SELECT payload_id FROM core_payloads WHERE status = 'ACTIVE'"
                " AND expires_at_ms IS NOT NULL AND expires_at_ms <= ?",
                (now_ms,),
            ).fetchall()
            purged = tuple(row["payload_id"] for row in rows)
            self._conn.execute(
                "UPDATE core_payloads SET data = '', status = 'PURGED' WHERE status = 'ACTIVE'"
                " AND expires_at_ms IS NOT NULL AND expires_at_ms <= ?",
                (now_ms,),
            )
        return purged

    # -- producer dependency graph ------------------------------------------

    def add_dependency(self, child: RecordRef, parent: RecordRef, kind: DependencyKind) -> None:
        """Insert an edge, rejecting self-edges and cycles (acyclic producer
        graph, handoff §7 core.dependencies)."""
        if child.id == parent.id:
            raise DependencyCycleError("self-dependency is not allowed")
        with _tx(self._conn) as connection:
            cycle = connection.execute(
                """
                WITH RECURSIVE ancestors(node) AS (
                    SELECT :parent_id
                    UNION
                    SELECT d.parent_ref FROM core_dependencies d
                    JOIN ancestors ON d.child_ref = ancestors.node
                )
                SELECT 1 FROM ancestors WHERE node = :child_id LIMIT 1
                """,
                {"parent_id": parent.id, "child_id": child.id},
            ).fetchone()
            if cycle is not None:
                raise DependencyCycleError(f"edge {child.id} -> {parent.id} would create a cycle")
            connection.execute(
                "INSERT INTO core_dependencies (child_ref, parent_ref, dependency_kind)"
                " VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (child.id, parent.id, kind.value),
            )

    def parents_of(self, record_id: str) -> tuple[str, ...]:
        rows = self._conn.execute(
            "SELECT DISTINCT parent_ref FROM core_dependencies WHERE child_ref = ?",
            (record_id,),
        ).fetchall()
        return tuple(row["parent_ref"] for row in rows)

    def children_of(self, parent_id: str) -> tuple[str, ...]:
        """Direct dependents of one record (indexed; invalidation walks
        these, handoff §7 core.dependencies)."""
        rows = self._conn.execute(
            "SELECT DISTINCT child_ref FROM core_dependencies WHERE parent_ref = ?"
            " ORDER BY child_ref",
            (parent_id,),
        ).fetchall()
        return tuple(row["child_ref"] for row in rows)

    # -- audit ---------------------------------------------------------------

    def audit(
        self,
        *,
        operation: str,
        entity_ref: str,
        reason_code: str,
        version_refs: tuple[str, ...] = (),
        at_ms: int,
    ) -> None:
        """Opaque refs and registry codes only — never payload bytes or
        free-form reasoning (handoff §7 core.audit)."""
        for value in (operation, entity_ref, reason_code):
            if not isinstance(value, str) or len(value) > 128 or "\n" in value:
                raise ValidationError("audit fields are bounded single-line strings", path="audit")
        if reason_code not in REASON_CODES:
            raise ValidationError(
                f"audit reason code {reason_code!r} is outside the closed registry",
                path="reason_code",
            )
        with _tx(self._conn):
            self._conn.execute(
                "INSERT INTO core_audit (operation, entity_ref, reason_code, version_refs, at_ms)"
                " VALUES (?, ?, ?, ?, ?)",
                (operation, entity_ref, reason_code, json.dumps(list(version_refs)), at_ms),
            )

    # -- work ----------------------------------------------------------------

    def create_work(
        self,
        *,
        work_id: str,
        trigger_key: str,
        intent_ref: str,
        episode_ref: str | None,
        runtime_epoch: int,
    ) -> bool:
        """Idempotent on trigger_key: returns True if newly created."""
        with _tx(self._conn) as connection:
            return self._create_work_tx(
                connection,
                work_id=work_id,
                trigger_key=trigger_key,
                intent_ref=intent_ref,
                episode_ref=episode_ref,
                runtime_epoch=runtime_epoch,
            )

    # -- release reservations (M3 D4 / I15) ----------------------------------

    def _reserve_release_tx(
        self,
        connection: sqlite3.Connection,
        *,
        effect_id: str,
        constraint_group_id: str,
        query_key: str,
        units: int,
        limit: int,
    ) -> bool:
        """Check-and-reserve one effect's release allowance inside the
        caller's BEGIN IMMEDIATE transaction (D4): SUM(accounting_units)
        over spending rows for the same constraint group and window
        query_key, inserting this effect's row only when the sum stays
        within the fixture limit. Idempotent per effect — an existing row
        for (effect_id, constraint_group_id) means the allowance is
        already held, so a retry re-enters without spending twice."""
        existing = connection.execute(
            "SELECT state FROM core_release_reservations"
            " WHERE effect_id = ? AND constraint_group_id = ?",
            (effect_id, constraint_group_id),
        ).fetchone()
        if existing is not None and existing["state"] in (
            "RESERVED",
            "COMMITTED",
            "POSSIBLY_COMMITTED",
        ):
            return True  # allowance already held; a retry re-enters without spending twice
        spent = connection.execute(
            "SELECT COALESCE(SUM(accounting_units), 0) FROM core_release_reservations"
            " WHERE constraint_group_id = ? AND query_key = ?"
            " AND state IN ('RESERVED', 'COMMITTED', 'POSSIBLY_COMMITTED')",
            (constraint_group_id, query_key),
        ).fetchone()[0]
        if spent + units > limit:
            return False
        if existing is not None:
            # a superseded revision released its unspent allowance; the
            # replacement spends fresh, reactivated in the current window
            connection.execute(
                "UPDATE core_release_reservations SET state = 'RESERVED', query_key = ?"
                " WHERE effect_id = ? AND constraint_group_id = ?",
                (query_key, effect_id, constraint_group_id),
            )
            return True
        connection.execute(
            "INSERT INTO core_release_reservations (reservation_id, effect_id,"
            " constraint_group_id, query_key, state, accounting_units)"
            " VALUES (?, ?, ?, ?, 'RESERVED', ?)",
            (
                f"release:{effect_id}:{constraint_group_id}",
                effect_id,
                constraint_group_id,
                query_key,
                units,
            ),
        )
        return True

    def settle_release(self, effect_id: str, state: str) -> None:
        """Settle an effect's RESERVED rows to the addendum's terminal
        states: COMMITTED on confirmed delivery, POSSIBLY_COMMITTED on
        UNKNOWN (never auto-refunded), CANCELLED_BEFORE_DISPATCH on
        acknowledged pre-dispatch cancellation. A confirmed-no-effect
        failure keeps its allowance spent (the attempt consumed the
        window slot; the single retry rides the same reservation)."""
        allowed = {"COMMITTED", "POSSIBLY_COMMITTED", "CANCELLED_BEFORE_DISPATCH"}
        if state not in allowed:
            raise StoreError(f"unknown release settlement state {state}")
        with _tx(self._conn):
            self._conn.execute(
                "UPDATE core_release_reservations SET state = ? WHERE effect_id = ?"
                " AND state = 'RESERVED'",
                (state, effect_id),
            )

    def _create_work_tx(
        self,
        connection: sqlite3.Connection,
        *,
        work_id: str,
        trigger_key: str,
        intent_ref: str,
        episode_ref: str | None,
        runtime_epoch: int,
    ) -> bool:
        existing = connection.execute(
            "SELECT work_id FROM core_work WHERE trigger_key = ?", (trigger_key,)
        ).fetchone()
        if existing is not None:
            return False
        connection.execute(
            "INSERT INTO core_work (work_id, trigger_key, intent_ref, episode_ref, state,"
            " lease_owner, lease_until_ms, runtime_epoch, main_steps_used, submissions_used,"
            " terminal_outcome_ref) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 0, 0, NULL)",
            (work_id, trigger_key, intent_ref, episode_ref, WorkState.READY.value, runtime_epoch),
        )
        return True

    def ready_work_ids(self) -> tuple[str, ...]:
        """READY work ids in trigger-key order (deterministic scheduling)."""
        rows = self._conn.execute(
            "SELECT work_id FROM core_work WHERE state = 'READY' ORDER BY trigger_key"
        ).fetchall()
        return tuple(row["work_id"] for row in rows)

    def get_work(self, work_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM core_work WHERE work_id = ?", (work_id,)).fetchone()
        return None if row is None else dict(row)

    def work_by_trigger_key(self, trigger_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM core_work WHERE trigger_key = ?", (trigger_key,)
        ).fetchone()
        return None if row is None else dict(row)

    def transition_work(
        self,
        work_id: str,
        new_state: WorkState,
        *,
        expected_state: WorkState | None = None,
    ) -> None:
        legal = {
            (WorkState.READY, WorkState.RUNNING),
            (WorkState.RUNNING, WorkState.COMPLETED),
            (WorkState.RUNNING, WorkState.HOLD),
            (WorkState.RUNNING, WorkState.READY),  # lost lease on restart (§6.1)
            (WorkState.HOLD, WorkState.READY),  # named external change resumes
            (WorkState.HOLD, WorkState.RUNNING),
        }
        with _tx(self._conn) as connection:
            row = connection.execute(
                "SELECT state FROM core_work WHERE work_id = ?", (work_id,)
            ).fetchone()
            if row is None:
                raise TransitionError(f"work {work_id} not found")
            current = WorkState(row["state"])
            if expected_state is not None and current != expected_state:
                raise TransitionError(
                    f"work {work_id} is {current.value}, expected {expected_state.value}"
                )
            if (current, new_state) not in legal:
                raise TransitionError(
                    f"work transition {current.value} -> {new_state.value} is not legal"
                )
            connection.execute(
                "UPDATE core_work SET state = ? WHERE work_id = ?",
                (new_state.value, work_id),
            )

    # -- ingestion ------------------------------------------------------------

    def _ingest_row_exists(
        self, consumer_id: str, source_event_id: str, source_revision: int
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT journal_seq FROM core_ingestion WHERE consumer_id = ?"
            " AND source_event_id = ? AND source_revision = ?",
            (consumer_id, source_event_id, source_revision),
        ).fetchone()

    def _insert_ingestion_row(
        self,
        connection: sqlite3.Connection,
        *,
        consumer_id: str,
        source_event_id: str,
        source_revision: int,
        journal_seq: int,
        fragment_refs: tuple[str, ...],
        recovery: str,
        ingested_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO core_ingestion (consumer_id, source_event_id, source_revision,"
            " journal_seq, fragment_refs, recovery, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                consumer_id,
                source_event_id,
                source_revision,
                journal_seq,
                json.dumps(list(fragment_refs)),
                recovery,
                ingested_at,
            ),
        )

    def set_cursor(self, cursor: int) -> None:
        self.set_meta("cursor", cursor)
