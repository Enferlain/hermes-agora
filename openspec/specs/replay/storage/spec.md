## Purpose

Durable two-domain storage for the replay runtime: versioned records, crash-safe ingestion, virtual time, epochs/generations, and deterministic fault injection. Core and transport are separate SQLite ownership domains with separate transaction commits and no claimed cross-domain atomicity.

## Requirements

### Requirement: Append-only versioned records

Records are stored keyed by (record_id, revision) with exactly one current revision per id; historical revisions are never edited in place.

#### Scenario: Second revision appended

- **WHEN** a record is stored with a new revision
- **THEN** the previous revision remains readable unchanged and only the new revision is current

#### Scenario: In-place historical edit attempted

- **WHEN** store operations would modify an existing (record_id, revision) row
- **THEN** the operation fails with CONFLICT

### Requirement: Schema-checked startup

Each store refuses to open a database whose schema version it does not know; migrations are explicit.

#### Scenario: Unknown schema version

- **WHEN** a store opens a database with an unrecognized schema version
- **THEN** startup fails rather than guessing a migration

### Requirement: Virtual time and durable epochs

Lease/budget/retention transitions use a virtual monotonic millisecond clock distinct from display UTC time that never moves backwards within a run; core runtime_epoch and sidecar transport_generation are durable monotonically increasing integers advanced by their respective restarts.

#### Scenario: Clock cannot move backwards

- **WHEN** the virtual clock is advanced by a non-positive delta within a run
- **THEN** the operation fails with INVALID_SCHEMA

#### Scenario: Core restart fences old epochs

- **WHEN** the core restarts and increments runtime_epoch
- **THEN** records carrying the old epoch are rejected as STALE_EPOCH by version-checked operations

### Requirement: Crash-safe ingestion

Ingesting a batch stores event refs, creates or dedupes work items, and advances a contiguous durable cursor in one core transaction committed before transport acknowledgement.

#### Scenario: Crash before acknowledgement

- **WHEN** the process is interrupted after the ingestion transaction commits but before the transport acknowledgement
- **THEN** after restart the batch is not re-applied (no duplicate work) and the cursor already reflects it; the retried batch is acknowledged as a duplicate

#### Scenario: Out-of-order batch

- **WHEN** a batch arrives whose first sequence number is beyond the cursor plus one
- **THEN** the cursor does not advance past the missing sequence and ingestion reports the gap

#### Scenario: Missing retained journal entries

- **WHEN** journal entries the cursor would advance over no longer exist
- **THEN** a GAP record is emitted with explicit resynchronization rather than pretended completeness

### Requirement: Out-of-line payloads

Personal or derived text is stored as bounded out-of-line payloads referenced by id, carrying sensitivity/commitment references and expiry; purging removes bytes while retaining permitted metadata references.

#### Scenario: Payload purge

- **WHEN** a payload reaches its virtual-time expiry and is purged
- **THEN** its bytes are no longer retrievable while record history retains the payload id and digest references

### Requirement: Acyclic producer dependencies

The dependencies table stores producer-input, declared, and control edges and rejects cycles.

#### Scenario: Circular producer graph

- **WHEN** a dependency insertion would create a cycle
- **THEN** the insertion fails with CONFLICT and no edge is written

### Requirement: Safe audit rows

Audit rows carry opaque operation/entity references, safe reason codes, and version/time references — never secret payloads, free-form reasoning, or necessity text.

#### Scenario: Audit write with unsafe content

- **WHEN** an audit write includes payload bytes or unbounded free-form text
- **THEN** the write fails validation

### Requirement: Named fault checkpoints

The eight named crash checkpoints are registered injection points; an armed checkpoint deterministically interrupts the flow at exactly that boundary when reached.

#### Scenario: Checkpoint armed at ingestion boundary

- **WHEN** the AFTER_INGEST_BEFORE_ACK checkpoint is armed and an ingestion batch reaches the post-commit/pre-ack boundary
- **THEN** the process interrupts at that boundary and restart recovery satisfies the crash-safe ingestion scenarios
