# Proposal: add-m1-replay-turn

## Why

The repository has settled architecture contracts (v0.2 addendum + implementation handoff) and a working project skeleton (M0), but no runtime code. ROADMAP.md M1 is the first implementation milestone: one deterministic end-to-end turn — event or timer, autonomous decision, fake effect or quiet outcome — without an LLM, on real durable stores from the start.

## What Changes

- Implements the full handoff §4 record set as validated immutable typed records with the common header rules, strict validation (unknown fields/enums fail, bounded lists/IDs, positive revisions), canonical payload binding (sorted-key UTF-8 JSON, SHA-256 digests), runtime-only field discipline, and the closed safe reason-code registry.
- Implements the two-domain persistence layer (core.sqlite / transport.sqlite, WAL) with the exact handoff §7 tables: versioned records with unique current revisions, out-of-line payloads with retention fields, scoped memory index, acyclic producer dependency graph, ingestion journal with contiguous cursor, work dedup, release reservations, dispatch leases, audit without secret payloads, and schema-version-checked startup that refuses unknown versions.
- Implements the virtual monotonic clock (separate from display UTC), durable runtime_epoch / transport_generation, and the named fault-checkpoint injection mechanism.
- Implements crash-safe ingestion: event refs + deduped work + cursor advance commit in one core transaction before transport acknowledgement; out-of-order batches cannot advance past a missing sequence; gaps are recorded explicitly.
- Wires the whole-turn path: coordinator, context/memory, scripted main/review/perception ports, ordinary CommitGate routing, fake sidecar and fake world with durable visibility evidence.
- Demonstrates the four M1 outcomes deterministically: ordinary message, reaction, explicit no action, timer-driven voluntary initiation — nothing converts an internal outcome or error into a public acknowledgement.

## Capabilities

### New Capabilities

- `replay/record-contracts`: validated typed domain records, canonical payload binding, runtime-only field discipline, and the safe reason-code registry defined by the v0.2 handoff.
- `replay/storage`: the two SQLite ownership domains, durable versioned records, crash-safe ingestion with a contiguous cursor, virtual clock, epochs/generations, and fault-checkpoint injection.
- `replay/whole-turn`: the deterministic end-to-end turn across coordinator → context/memory → scripted main → CommitGate → fake sidecar/world, including the four required outcome demonstrations.

### Modified Capabilities

(none — first change; no existing specs)

## Impact

- New modules `agora/contracts.py`, `agora/stores.py`, `agora/ports.py`, plus the wiring modules (`agora/context_memory.py`, `agora/coordinator.py`, `agora/commit_gate.py`, `agora/fake_sidecar.py`, `agora/runner.py`) and a replay CLI entry point.
- No runtime dependencies added (stdlib only).
- Tests under `tests/` covering handoff §10 order item 1 (types and records), ingestion crash-before-ack, store invariants, and the four outcome traces.
- The owning design remains docs/architecture/v0.2 (addendum, then handoff); this change narrows supported operations per ROADMAP.md M1 but does not reduce record fidelity.
