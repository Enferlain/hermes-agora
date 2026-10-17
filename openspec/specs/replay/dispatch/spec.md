## Purpose

Dispatch-time safety for the synthetic replay slice: what an authorized effect must re-prove between reservation and the durable DISPATCH_STARTED, how a shared logical effect id may be requeued, how release allowances are spent, and how delivery failures resolve. The gate's authorization is necessary and stays necessary — it is never sufficient at dispatch time.

## Requirements

### Requirement: Pre-dispatch revalidation

Inside the serialized reserve/consume window, before the durable DISPATCH_STARTED, the dispatcher re-reads the versions the authorization was bound to: the authorization (current, ACTIVE), the policy/commitment revisions its policy_version_vector binds — everything routing consulted, regardless of route — the snapshot (current, ACTIVE, not INVALID), a reaction's target (still the current revision), the audience (disclosure revision and visibility unchanged), and the capability (ACTIVE). A mismatch holds the outbox row with the matching typed code and nothing is dispatched; a change ordered after DISPATCH_STARTED is too late for prevention and is handled conservatively.

#### Scenario: Policy revision before dispatch

- **WHEN** a relevant policy revision lands after authorization but before dispatch starts
- **THEN** the queued effect holds with POLICY_CHANGED and the world receives nothing

#### Scenario: NONE-route authorization meets a tightening commitment

- **WHEN** a commitment consulted by routing was permissive at authorization (route NONE) and is revised to prohibit expression before dispatch starts
- **THEN** the queued effect holds with POLICY_CHANGED — routing evidence is revalidated, not just the routing result — and the world receives nothing

#### Scenario: Edited reaction target

- **WHEN** the target a reaction cites is edited after authorization
- **THEN** dispatch holds with TARGET_CHANGED rather than reacting to stale bytes

#### Scenario: Too late for prevention

- **WHEN** a relevant change lands after the durable DISPATCH_STARTED
- **THEN** the in-flight dispatch is not silently prevented; recovery is conservative (UNKNOWN semantics)

### Requirement: Acknowledged supersession

A proposal revision N+1 for a logical effect whose prior outbox row is not terminal-SENT requeues only after durable cancellation is acknowledged; TOO_LATE refuses the requeue and the earlier revision's outcome stands. Exact duplicate submits return the current state; conflicting bytes for the same logical id and revision return CONFLICT.

#### Scenario: r1 cancelled, r2 proceeds

- **WHEN** a revised plan supersedes an authorized-but-undispatched r1 and the cancellation is acknowledged
- **THEN** only r2's bytes reach the world, under the same logical effect id

#### Scenario: Too late to supersede

- **WHEN** the prior revision has already started dispatching
- **THEN** the requeue is refused with a typed outcome and no duplicate intentional send occurs

### Requirement: Release limits check and reserve atomically

Fixture-seeded capability rate limits are checked and reserved in one core transaction keyed by account, destination, and window; concurrent queued releases cannot both spend the same allowance, and an unreservable effect stays HELD/QUEUED rather than dispatching over budget.

#### Scenario: Concurrent releases

- **WHEN** two queued effects for the same destination compete for the last allowance in a window
- **THEN** exactly one dispatches and the other stays queued with RELEASE_LIMIT

#### Scenario: Window rollover

- **WHEN** the virtual clock advances into a new window
- **THEN** a previously unreservable queued effect can reserve and dispatch

### Requirement: Retry and reconciliation rules

FAILED_CONFIRMED may retry exactly once, only with evidence of no effect plus still-valid policy and dependencies, under a fresh attempt id with the logical effect id preserved. UNKNOWN is never blindly retried: only positive world evidence resolves it — APPLIED resolves to SENT, CERTIFIED_NOT_APPLIED to FAILED_CONFIRMED, INDETERMINATE remains UNKNOWN.

#### Scenario: Confirmed failure retries once

- **WHEN** the world confirms a failure with no applied bytes and dependencies remain valid
- **THEN** exactly one retry under a new attempt id may deliver, and no third attempt exists

#### Scenario: UNKNOWN reconciles or stays

- **WHEN** delivery timed out and the world reports APPLIED, CERTIFIED_NOT_APPLIED, or INDETERMINATE
- **THEN** the outbox resolves accordingly, and INDETERMINATE never re-sends

### Requirement: Dispatch invalidation from corrections and revocation

A queued effect whose authorization, snapshot, or producing view or memory is invalidated by a correction or revocation holds or cancels before dispatch; invalidation is not limited to retrieval-time eligibility.

#### Scenario: Correct after authorize

- **WHEN** a memory backing a queued effect's snapshot is corrected after authorization
- **THEN** the effect holds or cancels and the world receives the stale-derived bytes never
