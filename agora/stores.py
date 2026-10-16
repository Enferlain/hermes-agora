"""Two-domain durable storage for the replay runtime (handoff §7).

``CoreStore`` owns identity, policy, intents, memory, contexts, plans,
reservations, leases and audit in ``core.sqlite``; ``TransportStore`` owns
the journal, payloads, outbox and attempts in ``transport.sqlite``. The two
commit separately with no claimed cross-domain atomicity. Stores never read
clocks — all times are parameters — and never generate prompts or effects.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agora.contracts import (
    ANY_TARGET,
    REASON_CODES,
    RECORD_TYPES,
    ConversationEpisode,
    DependencyKind,
    EventKind,
    InboundEvent,
    MemoryRecord,
    MemoryStage,
    PayloadStatus,
    ReasonCode,
    Record,
    RecordRef,
    RecoveryStatus,
    ValidationError,
    WorkState,
    canonical_json_bytes,
    iter_ref_checks,
    record_from_payload,
    record_to_payload,
    strict_json_loads,
)
from agora.ports import CrashCheckpoint

SCHEMA_VERSION = 1

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


class StoreError(Exception):
    """Base class for storage-layer failures."""


class SchemaVersionError(StoreError):
    """The database's schema version is unknown or mismatched; startup refuses."""


class RecordConflictError(StoreError):
    """A versioned-record invariant was violated (in-place edit, duplicate)."""


class TransitionError(StoreError):
    """A state transition or fencing check failed (STALE_EPOCH and friends)."""


class InvalidReferenceError(StoreError):
    """A RecordRef target is missing or has the wrong record type (handoff
    §7 core.records typed refs). Raised inside the insert transaction, so
    nothing is written."""

    reason_code = ReasonCode.INVALID_REFERENCE

    def __init__(self, field: str, ref: RecordRef, detail: str) -> None:
        super().__init__(f"{field} cites {ref.id} revision {ref.revision} {detail}")
        self.field = field
        self.ref = ref
        self.detail = detail


class DependencyCycleError(StoreError):
    """A dependency insertion would create a producer-graph cycle."""


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


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.row_factory = sqlite3.Row
    return connection


def _open_or_create(path: Path, ddl: str, meta_table: str) -> tuple[sqlite3.Connection, bool]:
    """Open (or create) a domain database with schema checking.

    Returns the connection and whether the database was freshly created.
    Unknown or mismatched schema versions refuse startup; there are no
    implicit migrations (handoff §7 core.meta).
    """
    connection = _connect(path)
    digest = hashlib.sha256(ddl.encode("utf-8")).hexdigest()
    has_meta = (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (meta_table,)
        ).fetchone()
        is not None
    )
    if not has_meta:
        other_tables = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
        if other_tables:
            raise SchemaVersionError(
                f"{path}: tables exist but {meta_table} has no schema_version; refusing startup"
            )
        connection.executescript(ddl)
        with _tx(connection):
            connection.execute(
                f"INSERT INTO {meta_table} (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                f"INSERT INTO {meta_table} (key, value) VALUES ('schema_digest', ?)", (digest,)
            )
        return connection, True
    stored = connection.execute(
        f"SELECT value FROM {meta_table} WHERE key = 'schema_version'"
    ).fetchone()
    if stored is None:
        # A crash between DDL creation and the meta insert leaves tables
        # with no version row. That is completable only when every domain
        # table is still empty; otherwise startup refuses.
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " AND name != ?",
            (meta_table,),
        ).fetchall()
        for row in rows:
            count = connection.execute(f'SELECT count(*) FROM "{row["name"]}"').fetchone()[0]
            if count:
                raise SchemaVersionError(
                    f"{path}: tables hold data but {meta_table} has no schema_version;"
                    " refusing startup"
                )
        with _tx(connection):
            connection.execute(
                f"INSERT INTO {meta_table} (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                f"INSERT INTO {meta_table} (key, value) VALUES ('schema_digest', ?)", (digest,)
            )
        return connection, True  # completed initialization behaves as fresh
    if int(stored["value"]) != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"{path}: schema version {stored['value']} is not the known version {SCHEMA_VERSION}"
        )
    stored_digest = connection.execute(
        f"SELECT value FROM {meta_table} WHERE key = 'schema_digest'"
    ).fetchone()
    if stored_digest is None or stored_digest["value"] != digest:
        raise SchemaVersionError(f"{path}: schema digest mismatch; refusing startup")
    return connection, False


@contextmanager
def _tx(connection: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    if connection.in_transaction:
        raise StoreError("nested transactions are not supported")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


class _Domain:
    _meta_table: str

    def __init__(self, path: Path, ddl: str) -> None:
        self.path = Path(path)
        self._conn, self._fresh = _open_or_create(self.path, ddl, self._meta_table)

    @property
    def fresh(self) -> bool:
        return self._fresh

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        """One explicit transaction; any exception (InjectedCrash included)
        rolls back — which is the simulated process death path."""
        with _tx(self._conn) as connection:
            yield connection

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            f"SELECT value FROM {self._meta_table} WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str | int) -> None:
        with _tx(self._conn):
            self._set_meta_tx(key, value)

    def _set_meta_tx(self, key: str, value: str | int) -> None:
        self._conn.execute(
            f"INSERT INTO {self._meta_table} (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def close(self) -> None:
        self._conn.close()


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
            ("DISPATCH_STARTED", "UNKNOWN"),
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
