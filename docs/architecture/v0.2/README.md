# Persistent-agent architecture — v0.2 revision package

7 October 2026 · Documentation only

This is a traceable revision of the original package after its accepted adversarial review. It does not replace the research or implement the runtime.

Read in this order:

1. [Architecture v0.2 addendum](architecture-v0.2-addendum.md): decisions D01–D11, explicit resolution of F01/F02/F04/F05/F06/F07, and accepted IMPORTANT corrections that affect replay.
2. [Synthetic replay implementation handoff](synthetic-replay-implementation-handoff.md): exact records, ports, module ownership, state transitions, persistence, defaults, stubs, invariants, test order and extension questions.
3. [Revision manifest](revision-manifest.json): baseline/output hashes and finding-to-decision/test traceability.

The [original architecture package](../v0.1/) and [accepted adversarial review](../v0.1/adversarial-architecture-review.md) are preserved byte-for-byte as the extracted v0.1 tree. Their contents are not rewritten. The manifest records which original provisions are superseded. Precedence is addendum, then handoff, then the original package for unaffected matters.

The revised slice starts with scripted synthetic replay. It tests one autonomous identity across rooms, DM and scheduled work, purpose-separated authority, runtime provenance, controlled views, selective review and restart-safe fake dispatch. It uses modules and separate SQLite ownership domains, without requiring multiple services, a live Discord account or Clef.

The agent may originate intentions and ordinary speech without operator approval. Purpose cannot mint resource rights. Every enabled effect crosses CommitGate, while an additional model assessment is selective. Ordinary private knowledge remains usable under contextual discretion. Strong restrictions receive explicit admission/review rules; none is advertised as a proof of semantic noninterference.

**Implementation readiness:** the synthetic contracts are settled. A fresh Hermes coding session can begin in an isolated replay namespace using the handoff. It must inspect the actual checkout and keep unsupported capabilities inert. Passing replay establishes structural invariants and failure behavior; model-backed privacy, local inference performance and live transport security remain unproven. No production code was created in this revision.
