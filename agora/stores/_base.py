"""Shared storage plumbing for both domains (handoff §7): exceptions,
connection setup with explicit schema checking, the BEGIN IMMEDIATE
transaction helper, and the two-domain base class."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from agora.contracts import ReasonCode, RecordRef

SCHEMA_VERSION = 1


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
