# Design: add-m2-views-discretion

## Context

See proposal.md. The evaluation semantics are pinned by the v0.2 handoff (§4.2 records — already implemented and persisted by M1 — §8 routing, §6.3 memory lifecycle) and addendum D01–D08; this design covers only the genuinely open implementation choices. Records, stores, canonical binding, coverage plumbing, and the whole-turn path exist and are tested — M2 is a delta against a working system.

## Goals / Non-Goals

**Goals:**

- Implement evaluation (not redesign): authority matching, access results/views, transforms, memory lifecycle, routing, review protocol — each traceable to a handoff clause.
- Seed the five resource classes as fixture data through the trusted manifest port.
- Land typed-ref verification first so every subsequently-written record benefits.

**Non-Goals:**

- No real confidentiality norms, no Clef/perception (M7), no remote models, no embeddings/global retrieval index, no erasure semantics, no numeric privacy budget beyond fixed query/transform constraints (all §11 deferred).
- No schema/DDL changes; core.memory_scope already exists from M1.

## Decisions

### D1 — Authority evaluation lives in context_memory behind a pure evaluator

Handoff §2 assigns "access views" and "compute admission" to context_memory. Concretely: a pure `AuthorityEvaluator` (records in, `AccessVerdict` out — no DB, no clock) plus `ContextMemory.request_access(request, runtime_origin)` which loads registries, calls the evaluator, constructs the MemoryAccessResult/MemoryView records, persists them (necessity text out-of-line via `text_field_refs`), audits, and returns the typed result. Pure core + record-building shell keeps the evaluation property-testable and the store boundary in one place.

### D2 — Evaluation order is fixed and fail-closed

Per v0.1's carried-forward evaluation order: validate references → registry lookup (missing registry id → deny protected matching) → binding/eligibility (issuer quorum per approval rule) → authorization dimension match (operation, data class, precision, transforms, purpose class, audience bound, validity) → conflict detection (contradictory active commitments/policies → CONFLICT before PENDING) → transform admission → view construction. Every branch produces one of the five statuses with registry reason codes; there is no "default allow" path.

### D3 — Transforms are pure functions + certificate records

`transforms.py`-style small registry inside context_memory (two functions, registry ids, fixture-fixed parameters from the manifest). A transform run: loads the authorized FULL/fields view of the source, computes the output payload out-of-line, writes a TransformCertificate naming discharged vs retained commitments, and builds the output MemoryView citing the certificate. No model text, no thresholds from anywhere but the manifest.

### D4 — Memory eligibility as store queries over core.memory_scope

M1 already persists MemoryRecord rows; M2 adds the scope index rows (memory_id, revision, stage, scope_kind/ref, status, expiry) written in the same transaction as record promotion, queried by eligibility (stage/status/expiry + scope match) before any payload read. Corrections: new assertion revision + dependency invalidation via the existing acyclic dependency graph (snapshots and derivations already register PRODUCER_INPUT edges from M1), plus status flips to STALE for derivations whose input_version_vector no longer matches.

### D5 — Routing table in commit_gate, review flow through ports

`CommitGate.evaluate` gains the §8 ordered checks: the existing code checks (M1) → routing per the exact first-slice table → REQUIRED builds the ReviewEnvelope (closed set per the 2026-10-12 note), calls `ReviewerPort.assess` (scripted in fixtures), verifies the assessment's digest/version binding, rechecks versions after any model wait, then authorizes or holds. `ReviewRequirement`/`ReviewAssessment` records persist per the M1 contracts (assessment ids embed plan+revision — the D03 one-assessment-per-submission cap is already structural). No downgrade: a resubmission after HOLD routes fresh with proposal_revision+1.

### D6 — Typed-ref verification via a REF_TARGETS map at put_record

A hand-authored `REF_TARGETS: dict[type[Record], dict[str, str | set[str]]]` (field → expected TYPE_NAME(s)) consulted by `put_record` inside the insert transaction: each RecordRef field's target must exist as a current record of the expected type. Failures raise INVALID_REFERENCE before any write. Nullable refs skip when None. The map is codec-derived-checked by a test that walks field plans so it can't drift. `forward refs` (target written later in one fixture load) are handled by validating after all loads in the fixture loader, not by weakening put_record.

### D7 — Fixtures: the five resource classes in the manifest

RunManifest grows a `registries` section (principals, policies, authority bindings, use authorizations, capability commitments, transforms with fixed parameters, resources with payload values and scope/commitment metadata) loaded through the fixture port at bootstrap. Unknown registry ids fail startup (existing M1 behavior). Paired-world adversarial fixtures (same observable context, different private fact) are manifest variants selected by fixture id.

## Risks / Trade-offs

- **AuthorityEvaluator complexity is inherent** (the dimension matrix is the point); the pure-core design keeps it exhaustively testable without stores.
- **Typed-ref verification adds an insert-time cost** and one more place records can be rejected; that is the desired fail-closed behavior, and the fixture-loader batching keeps legitimate forward refs workable.
- **Review routing's STRONG-commitment detection depends on coverage being threaded correctly** — M1 already computes producer coverage on every output, so the risk is using it, not building it; adversarial tests (declared-parent omission, private-derived goal) target exactly this.
- The release-ledger question is settled by the 2026-10-12 note; if overturned, ReviewEnvelope changes under a schema bump, not silently here.
