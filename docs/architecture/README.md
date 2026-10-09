# Architecture

This index defines the current architecture revision and document precedence for Hermes Agora.

## Current revision: v0.2

Read the v0.2 package in this order:

1. [v0.2 addendum](v0.2/architecture-v0.2-addendum.md) — decisions D01–D11 and the accepted resolution of review findings F01/F02/F04–F07.
2. [Synthetic replay implementation handoff](v0.2/synthetic-replay-implementation-handoff.md) — the normative contract for the first implementation: records, ports, state machines, persistence, invariants I01–I16, and test order.
3. [Revision manifest](v0.2/revision-manifest.json) — baseline/output hashes and finding-to-decision/test traceability.

## Precedence

For matters the v0.2 package addresses, precedence is:

1. v0.2 addendum
2. v0.2 implementation handoff
3. the v0.1 package, for unaffected matters

The v0.1 package is preserved byte-for-byte as historical evidence:

- [v0.1 package guide](v0.1/README.md)
- [Persistent-agent architecture](v0.1/persistent-agent-architecture.md)
- [Runtime contracts](v0.1/runtime-contracts.md)
- [Research evidence](v0.1/research-evidence.md)
- [Adversarial architecture review](v0.1/adversarial-architecture-review.md) — accepted; its corrections are recorded in the v0.2 addendum
- [Evaluation plan](v0.1/evaluation-plan.md)
- [Source manifest](v0.1/source-manifest.json)

Historical revisions are never rewritten to reflect newer decisions. Newer decisions are recorded in a new revision, an addendum, or a current implementation note.

## Related documents

- [ROADMAP.md](../../ROADMAP.md) — intended milestone sequencing and exit criteria. It proposes ordering; it cannot silently override the contracts above.
- [Implementation notes](../implementation-notes/README.md) — current-state implementation decisions (packaging, environment, workflow) that the historical architecture documents do not settle.
- [CHANGELOG.md](../../CHANGELOG.md) — notable completed changes.

## Implementation status

Pre-implementation. No runtime contracts are implemented yet; the first milestones (M0 foundation, then the M1 deterministic replay slice) are tracked in Beads.
