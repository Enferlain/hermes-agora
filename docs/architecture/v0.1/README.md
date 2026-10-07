# Persistent social agent architecture package

Prepared 7 October 2026. Research and implementation design; no production code, live Discord actions, or local model benchmark results.

Start with [persistent-agent-architecture.md](persistent-agent-architecture.md). It contains the thirteen requested deliverables: problem/non-goals, research map, threat model, behavioral/provenance/memory/System-One models, four alternatives, recommendation, code/state boundaries, prototype, open questions and roadmap.

Supporting files:

- [runtime-contracts.md](runtime-contracts.md): versioned types, event/request/action examples, ownership, IPC/recovery, capabilities, audit, and answers to all 25 implementation questions.
- [evaluation-plan.md](evaluation-plan.md): adversarial and ordinary-social suites, paired-world leakage tests, ablations, local inference benchmarks, fault injection and release gates.
- [research-evidence.md](research-evidence.md): primary sources, what experiments establish and do not establish, source-inspected donor implementations, commit pins and verification limits.
- [source-manifest.json](source-manifest.json): exact repository files, pinned source URLs and local evidence content hashes.

All architecture choices are proposals. Observed implementation/paper findings and inferences are labeled separately. The evidence ledger explicitly marks partially inspected research and unavailable integration repositories.

## Decisions a coding agent should preserve

One identity and logical memory graph; audience/task-specific context snapshots. A mention is a relevance signal. NO_ACTION is normal. Retrieval permission and expression permission are separate. Provenance has independent authority, identity, integrity, relationship, audience, ownership and lineage dimensions. Sensitive abstractions retain constraints. Memory promotion never implies public visibility.

Use a small persistent user-account sidecar for connection/journal/outbox ownership, with Hermes cognition and policy/review as modules. Model servers are performance boundaries. Do not build a fleet of policy services. Sidecar separation alone is not a hard credential boundary if unrestricted tools can read its files.

Main chooses participation and ordinary social discretion. System One supplies bounded signals. Fresh pre-commit review considers semantics and every enabled effect; owner/capability invariants cannot be overridden. Silence, reactions, timing and composed disclosures need explicit evaluation.

## First implementation milestone

Build a replay-only vertical slice with synthetic scoped memory, shared episodes, optional participation, purpose views, draft/review/suppression and a fake outbox. Benchmark the local sensor. Add the pinned selfcord sidecar only after the contracts and bypass/fault checks work. Keep transport feature expansion behind measured safety **and** social usefulness.

The key experiment is not whether a bot stops printing secret strings. It is whether the same continuous agent can use legitimate cross-context knowledge, withstand probing/poisoning, and still choose useful silence, reactions, jokes and initiations naturally.
