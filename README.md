# Hermes Agora

Persistent social runtime for Hermes.

Hermes Agora explores how a continuous autonomous agent can participate naturally in open human environments while preserving provenance, memory boundaries, contextual discretion, and control over externally observable actions.

Discord is the first target environment, but Agora is not intended to be a Discord bot framework.

## Goal

Most chat integrations assume:

```text
message arrives
    ↓
agent runs
    ↓
agent replies
```

Agora instead treats an external message as an event the agent may notice.

The agent may:

- ignore it
- react
- reply
- joke
- defer
- investigate something
- remember it
- initiate something later
- decide that sensitive information is relevant without deciding to disclose it

The central problem is not simply restricting what the model can access.

It is allowing one persistent agent to know information from many contexts while deciding, based on provenance, audience, relationships, purpose and its own judgment, what should become salient and what should become externally observable.

## Design principles

### One continuous agent

Discord does not create a separate persona or isolated "Discord brain."

The same agent identity may operate across local sessions, memory, tools, scheduled work, private conversations and social environments.

Continuity comes from durable agent state, not from blindly carrying model context or KV caches between audiences.

### Attention is not obligation

A mention, reply, DM or direct question means an event deserves attention.

It does **not** mean the agent must answer.

Participation is an agent decision.

### Provenance survives context construction

External content should not be flattened into indistinguishable text.

Runtime context preserves information such as:

```text
source
speaker
authenticated producer
audience
relationship
ownership / subjects
instruction authority
confidentiality
direct / quoted / inferred / summarized status
derivation lineage
```

A Discord message, private memory, tool result and durable instruction therefore remain distinguishable even when they appear in the same model context.

### Intent is separate from authority

The agent may create its own goals and interests.

An external message may inspire an idea and the agent may voluntarily adopt it.

That does not grant the message, its author, or the resulting intent authority over protected resources.

```text
agent wants to do X
        ≠
agent is authorized to use Y
```

### Sensitive memory is purpose-aware

Sensitive information is not necessarily hidden permanently.

When protected information would be useful, the agent can request a view for a specific purpose and context.

A request may resolve to:

```text
FULL
PARTIAL
ABSTRACTED
DENIED
```

Access and disclosure are separate decisions.

Being allowed to use a fact internally does not automatically authorize communicating that fact or allowing it to shape externally observable behavior.

### External actions cross a commit boundary

Model output never goes directly to transport.

Proposed effects pass through a common `CommitGate`.

```text
reason
  ↓
propose effect
  ↓
commit checks
  ↓
optional semantic review
  ↓
authorize exact effect
  ↓
dispatch
```

Ordinary low-risk conversation should not require an additional model inference merely to say something.

More sensitive or consequential flows may receive stronger review.

Hard resource/capability restrictions remain code-enforced and cannot be waived by model output.

### Auxiliary models are sensors, not governors

Fast decision models such as Clef or d1 may eventually provide cheap signals for:

- relevance
- conversation continuity
- likely participation value
- social mode
- possible probing
- privacy sensitivity
- prompt injection
- whether deeper reasoning is useful

These signals inform Hermes.

They do not become a second personality or an authority that decides what Hermes is allowed to think or say.

Their usefulness must be demonstrated experimentally.

## Architecture

The current design separates four primary concerns:

```text
                 ┌──────────────────┐
events ─────────►│ coordinator      │
                 └────────┬─────────┘
                          │
                          ▼
                 ┌──────────────────┐
                 │ context / memory │
                 │ provenance       │
                 │ protected views  │
                 └────────┬─────────┘
                          │
                          ▼
                 ┌──────────────────┐
                 │ Hermes           │
                 │ deliberation     │
                 └────────┬─────────┘
                          │
                    proposed effects
                          │
                          ▼
                 ┌──────────────────┐
                 │ CommitGate       │
                 │ authorization    │
                 │ review routing   │
                 └────────┬─────────┘
                          │
                          ▼
                 ┌──────────────────┐
                 │ transport        │
                 │ journal/outbox   │
                 └──────────────────┘
```

Transport is a lifecycle and delivery boundary, not a second mind.

## Current status

Agora is currently in the architecture/prototype stage.

The first implementation target is a deterministic replay environment using synthetic resources and a fake transport.

It is intended to validate:

- provenance propagation
- autonomous intent creation
- purpose-aware protected-memory views
- audience/context separation
- participation and `NO_ACTION`
- commit authorization
- review routing
- effect revision/cancellation
- journal/outbox recovery
- uncertain and partial delivery semantics
- ordinary social usefulness
- adversarial information leakage

Live Discord integration comes later.

## Initial implementation

The first vertical slice is intentionally small:

```text
agora/
├── contracts.py
├── stores.py
├── context_memory.py
├── coordinator.py
├── commit_gate.py
├── fake_sidecar.py
├── ports.py
└── runner.py
```

These boundaries are provisional.

They exist to test the architecture, not to predetermine the permanent implementation or language split.

Python is used initially because Hermes and the expected Discord adapter are Python-based and the first goal is architectural validation.

Deterministic components may later move to another language if stronger isolation, concurrency or correctness guarantees justify the added boundary.

## Evaluation

Safety is not measured only by searching outputs for literal secrets.

Tests should include behavioral leakage through:

- yes/no choices
- reactions
- omissions
- ranking
- silence
- timing
- proactive behavior
- sequences of individually harmless actions

Where useful, evaluation uses paired worlds in which a private fact changes while the attacker's observable context remains otherwise identical.

Social usefulness is evaluated separately.

An always-silent agent is not considered a successful privacy solution.

## Non-goals

Agora is not intended to:

- turn Hermes into a conventional command bot
- require human approval for every thought or social action
- make arbitrary external text trusted
- claim perfect confidentiality from probabilistic model behavior
- treat every private fact as permanently inaccessible
- make a decision model responsible for agent identity or judgment
- solve all model-level prompt injection through prompting alone

## Discord

The eventual Discord adapter is intended to support persistent participation using a normal user account.

User-account automation may violate Discord's terms of service and carries account risk.

The Discord transport is therefore treated as an adapter rather than as the definition of the architecture.

## Security model

Agora aims for layered defenses rather than a claim of perfect information noninterference.

Broadly:

```text
soft / contextual
    social discretion
    participation
    ordinary personal information
    relevance
    expression

hard / structural
    credentials
    capability custody
    instruction authority
    protected resource grants
    consequential operations
    exact effect authorization
```

The exact boundary is expected to evolve through testing.

## Development

The first milestone is the synthetic replay implementation.

Do not add live Discord transport, Clef/d1 integration, voice, attachment handling or broad host-tool access until the replay contracts and invariants are exercised.

See the architecture and implementation handoff documents for the current design.

## License

Apache-2.0
