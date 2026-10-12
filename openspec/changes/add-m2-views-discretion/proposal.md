# Proposal: add-m2-views-discretion

## Why

M1 proved the machinery (records, stores, whole-turn path) with everything ordinary; M2 adds the reason Agora exists — contextual discretion. ROADMAP M2's exit evidence: the access/use distinction, derived restrictions, and ordinary cross-context recall all tested in replay, with positive cases showing legitimate recall and ordinary speech alongside protected suppression. The spine already persists every record M2 needs; this change implements their evaluation semantics.

## What Changes

- Implements authority evaluation over the fixture registries: AuthorityBinding/ResourcePolicy/UseAuthorization matching with SINGLE_ISSUER/ALL_LISTED approval rules, domain-scoped operations (READ/TRANSFORM/REVIEW/STORE under RESOURCE_USE; EXPRESS/OBSERVABLE_INFLUENCE under DISCLOSURE), and the five distinct access statuses — no universal operator priority anywhere.
- Implements purpose-aware protected views: MemoryAccessRequest → MemoryAccessResult with GRANTED carrying FULL/PARTIAL/ABSTRACTED views (permitted uses, inherited commitments, transform basis), every non-GRANTED status carrying no value; the two fixed transforms (FIXED_FIELD_VIEW, FIXED_AFFORDABILITY_PREDICATE) with certificates that discharge only their specified commitments.
- Implements scoped memory: stage/scope/status/expiry eligibility, explicit staged promotion without scope widening, correction/supersession that invalidates affected snapshots and late derivations, and episode grouping that never confers scope rights.
- Implements the exact handoff §8 first-slice review routing — NONE/ADVISORY/REQUIRED/HARD_DENY — including REQUIRED for active STRONG commitments needing expression/observable-use resolution (relevant QUIET/DEFER included), the closed ReviewEnvelope per the 2026-10-12 release-ledger decision, PASS keyed to proposal/policy versions, and no downgrade after a failed required assessment.
- Adds store-level typed-ref target verification at record insert (beads hermes-agora-4za, sequenced first).
- Seeds the five handoff resource classes in replay fixtures: public project fact, agent-private ordinary preference, human financial protected fact, third-party confidential disclosure, joint protected record.

## Capabilities

### New Capabilities

- `replay/discretion`: authority evaluation, purpose-aware protected views with the two fixed transforms, scoped memory lifecycle, and selective review routing for the synthetic replay slice.

### Modified Capabilities

(none — whole-turn and storage requirements are unchanged; discretion composes with them)

## Impact

- Grows `agora/context_memory.py` (access evaluation, views, transforms), `agora/commit_gate.py` (routing, review envelope), `agora/coordinator.py` (REQUEST_ACCESS/PROPOSE_MEMORY step handling), `agora/stores.py` (typed-ref verification, memory-scope queries), and the runner fixtures/manifest.
- No new runtime dependencies; no schema-version change (typed-ref verification is insert-time logic, not DDL).
- The architecture contracts are owned by the v0.2 handoff §4.2/§8 and addendum D01–D08; this change implements them, it does not redesign them.
