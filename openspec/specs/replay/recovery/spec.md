## Purpose

Restart-safe replay composition for the synthetic slice: epoch/generation fencing, conservative recovery at the named crash checkpoints, deduplicated acceptance of resumed work, and deterministic replay acceptance across the three handoff stories.

## Requirements

### Requirement: Restart fencing

A core restart bumps the durable runtime epoch and a sidecar/transport restart advances the transport generation; stale epochs and generations reject submits, lease consumption, and dispatch starts with the typed codes, and queued effects hold until revalidation.

#### Scenario: Stale lease after restart

- **WHEN** a lease issued before a core restart is presented for consumption after it
- **THEN** consumption is refused with the stale-epoch code and nothing dispatches

### Requirement: Conservative crash recovery at named checkpoints

A crash before or after each core/transport/world boundary recovers to the conservative state: nothing acked that was not ingested, no receipt without a world outcome, and a DISPATCH_STARTED found at restart becomes UNKNOWN — queried against the world first, never guessed from local state.

#### Scenario: Crash between dispatch start and world

- **WHEN** a restart lands after the durable DISPATCH_STARTED but before the world call
- **THEN** the effect is UNKNOWN even if the world could have proven no send had it been asked

#### Scenario: Crash before acknowledgement

- **WHEN** a crash lands between core ingestion and the transport acknowledgement
- **THEN** replay re-ingests deduplicated and the cursor advances exactly once

### Requirement: Deduplicated acceptance with one identity

Resumed jobs, timers, and main-step acceptances deduplicate accepted state changes while preserving one continuous identity: re-running pending work after a restart produces the same records, not duplicates, and budgets/dedup keys are retained across lease loss.

#### Scenario: Repeated timer fires once

- **WHEN** a deferred intent's timer work is re-created after a restart
- **THEN** the trigger key deduplicates it into one READY item with counters retained

### Requirement: Deterministic replay acceptance

The three handoff stories run end-to-end — stranger-suggests-research with a restart continuation, the affordability probe with constrained-view paths, and the r1-reaction-superseded-by-r2 story with an edit and restart before dispatch — and identical manifests with identical fault plans produce byte-identical observer traces across re-runs.

#### Scenario: Story three under restart

- **WHEN** r1 is authorized, the target is edited, the core restarts, and r2 is proposed after an acknowledged cancellation
- **THEN** r2 proceeds only through fresh checks and exactly one reaction is visible

#### Scenario: Trace equality

- **WHEN** the same manifest and fault plan are replayed twice from scratch
- **THEN** the observer traces are byte-identical
