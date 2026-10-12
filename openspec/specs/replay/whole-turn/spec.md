## Purpose

One deterministic end-to-end turn: an inbound event or timer becomes an autonomous typed decision that becomes a fake effect or an explicit quiet outcome, on real durable stores, without an LLM.

## Requirements

### Requirement: Full path wiring

The turn path runs coordinator → context/memory → scripted main model → CommitGate → fake sidecar → fake world, with the fake sidecar and world as ordinary modules and two SQLite ownership domains from the start (no disposable in-memory path).

#### Scenario: Deterministic replay

- **WHEN** the same run manifest is replayed from the same origin
- **THEN** the observer trace and durable state are identical

### Requirement: CommitGate monopoly on effects

Every planned fake effect crosses the CommitGate; no alternate send path exists, and model output can never submit an authorization or invoke the sidecar or world directly.

#### Scenario: Attempted bypass

- **WHEN** any code path attempts to dispatch an effect without a CommitGate authorization
- **THEN** the sidecar rejects the submission

### Requirement: Exact effect bytes

Rendered SEND_MESSAGE and ADD_REACTION payloads use their exact defined shapes; the sidecar transmits those exact bytes with no automatic mentions, splitting, reply conversion, or prefix; digests bind the bytes through authorization and outbox records.

#### Scenario: Byte identity at the world boundary

- **WHEN** an authorized effect is dispatched
- **THEN** the fake world observes exactly the approved payload bytes

### Requirement: Four outcome demonstrations

A deterministic replay command demonstrates an ordinary message, a reaction, an explicit no action, and a timer-driven voluntary initiation. A direct mention may end silently; a scripted agent may answer without a mention when its interest makes participation worthwhile.

#### Scenario: Mention ends silently

- **WHEN** a directly addressed event is processed and the scripted decision is FINISH/NO_ACTION
- **THEN** no transport effect, typing indicator, fallback acknowledgement, or public error occurs, and the outcome is recorded as NO_ACTION

#### Scenario: Timer-driven initiation

- **WHEN** a deferred intent's timer becomes due
- **THEN** exactly one deduplicated wake occurs and the agent may initiate without any inbound event

### Requirement: Quiet outcomes stay typed and invisible

Internal outcomes and errors never automatically convert into public acknowledgement; NO_ACTION creates no outbox row.

#### Scenario: Error during deliberation

- **WHEN** a main-step error or budget exhaustion occurs
- **THEN** the turn records its typed outcome (FAILED_INTERNAL or BUDGET_EXHAUSTED with quiet completion) and produces no visible fallback

### Requirement: Unsupported operations stay inert

Operations outside the M1 surface (real Discord, remote models, shell/browser/HTTP/MCP, attachments, voice, edits, deletes, chunking, embeddings) return UNSUPPORTED and never invoke a donor implementation.

#### Scenario: Unsupported effect kind

- **WHEN** a plan proposes an effect kind outside SEND_MESSAGE and ADD_REACTION
- **THEN** the gate denies it with UNSUPPORTED

### Requirement: Configuration from the run manifest

Harness limits (main steps, submissions, effects per plan, dispatch lease duration, message length, turn and concurrency bounds) come explicitly from the validated run manifest, never from implicit defaults.

#### Scenario: Unknown registry id in manifest

- **WHEN** a run manifest references an id absent from its registries
- **THEN** startup fails rather than defaulting
