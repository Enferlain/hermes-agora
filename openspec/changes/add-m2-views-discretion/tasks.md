# Tasks: add-m2-views-discretion

## 1. Entry hardening

- [ ] 1.1 Typed-ref target verification at `put_record` (beads hermes-agora-4za): REF_TARGETS map, in-transaction INVALID_REFERENCE failures, codec-derived drift test, fixture-loader batch validation for forward refs. Verify: wrong-type and missing-target inserts fail with nothing written; all existing tests stay green.
- [ ] 1.2 Manifest registries: principals/policies/bindings/authorizations/commitments/transform parameters seeding the five handoff resource classes through the fixture port; unknown ids fail startup. Verify: a seeded replay boots and the registries round-trip from core.records.

## 2. Authority and views

- [ ] 2.1 Pure AuthorityEvaluator with the fixed evaluation order (references → registry → issuer quorum → authorization dimensions → conflict → transform admission); five access statuses with registry reason codes. Verify: property/unit battery covers every status, SINGLE_ISSUER vs ALL_LISTED, domain-scoped operations, no operator priority.
- [ ] 2.2 `ContextMemory.request_access` + view construction: result/view records persisted, permitted uses and inherited commitments from actual authorizations, necessity text out-of-line, audit rows registry-only. Verify: granted-partial and denied-nothing scenarios from the spec pass.
- [ ] 2.3 Transform registry: FIXED_FIELD_VIEW and FIXED_AFFORDABILITY_PREDICATE with fixture-fixed parameters; certificates discharging only named commitments. Verify: affordability predicate output exposes only the predicate; unregistered transform ids fail.

## 3. Memory lifecycle

- [ ] 3.1 Eligibility via core.memory_scope: scope-index rows written transactionally with record promotion; selection filters by stage/scope/status/expiry before payload reads; no global index. Verify: ordinary private memories stay eligible cross-context; ineligible payloads are never read.
- [ ] 3.2 Staged promotion and corrections: explicit promotion without scope widening; correction supersedes, invalidates snapshots/derivations via the dependency graph, blocks stale promotions on version-vector mismatch. Verify: stale-summary-after-correction and promotion-without-widening scenarios pass.
- [ ] 3.3 INTERNAL_ONLY containment: strong internal-only views structurally inadmissible to externally effect-capable snapshots (including DMs). Verify: admission failure at snapshot build, never at dispatch.

## 4. Review routing and protocol

- [ ] 4.1 §8 routing table in CommitGate: HARD_DENY conditions, REQUIRED on active STRONG expression/observable-use resolution (QUIET/DEFER included, runtime coverage closure), fixture-configured ADVISORY, NONE otherwise; sensor scores never create REQUIRED. Verify: ordinary-speech NONE, strong-restriction REQUIRED, declined-advisory scenarios pass.
- [ ] 4.2 REQUIRED protocol: closed ReviewEnvelope construction, scripted ReviewerPort flow, PASS bound to digests/versions, version-recheck after model wait, HOLD on CONCERNS/INSUFFICIENT_CONTEXT/missing facts/timeout, no downgrade on resubmission. Verify: version-change-voids-pass and facts-missing-hold scenarios pass.

## 5. Stories and closure

- [ ] 5.1 Coordinator integration: REQUEST_ACCESS and PROPOSE_MEMORY steps wired to the new evaluation/memory paths with runtime-origin binding. Verify: handoff story 2 (affordability probe) runs end-to-end with constrained-view, revise/joke/quiet paths.
- [ ] 5.2 Adversarial battery: paired-world fixtures, forged lineage/declared-parent omission, public-only regeneration, private→public context switch, and social-positive cases (legitimate cross-context recall; an always-silent implementation fails). Verify: all pass deterministically.
- [ ] 5.3 Closure: full gates green, CHANGELOG entry, review-agent pass with findings addressed, `bd close` hermes-agora-2xl.1 with M2 exit evidence per ROADMAP.
