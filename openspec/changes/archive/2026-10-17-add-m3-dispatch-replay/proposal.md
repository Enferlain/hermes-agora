# Proposal: add-m3-dispatch-replay

## Why

M1 built the whole-turn path with a single-shot dispatch skeleton (leases, attempts, UNKNOWN/reconcile, cancel hooks) and M2 completed discretion. ROADMAP M3's exit is the full v0.2 synthetic vertical slice: I01–I16, the three handoff stories, and the named crash checkpoints passing deterministically. The missing mass is dispatch-time safety (version revalidation, supersession, release limits, retry rules) and restart-safe replay composition — the pieces that make the slice honest about crashes instead of merely tolerant of their happy paths.

## What Changes

- Implements pre-dispatch revalidation inside the sidecar's reserve/consume window (I08/I10): current authorization/snapshot/target/audience/capability versions are re-checked before the durable DISPATCH_STARTED; a relevant change ordered before that transition prevents dispatch with a typed hold, and a change after it is too late for prevention (conservative recovery).
- Implements acknowledged supersession (I09): a revision N+1 plan for the same logical effect requeues only after the r1 effect's cancellation is acknowledged; TOO_LATE never requeues; exact duplicate submits return current state and conflicting bytes return CONFLICT.
- Implements dispatch-time target and audience change detection: an edited reaction target holds with TARGET_CHANGED; a material audience change (visibility/disclosure/unknown-reader flip) holds with AUDIENCE_CHANGED while harmless ordinary chatter still dispatches.
- Implements release limits (I15): fixture-seeded capability rate limits checked and reserved in one core transaction so concurrent queued releases cannot both spend the same allowance; unreservable effects stay HELD/QUEUED.
- Implements the retry and reconciliation rules (I11): FAILED_CONFIRMED may retry exactly once with no-effect evidence plus a fresh attempt id and still-valid dependencies; UNKNOWN is never blindly retried — only positive world evidence (APPLIED / CERTIFIED_NOT_APPLIED) resolves it; INDETERMINATE stays UNKNOWN.
- Extends correction/revocation invalidation to queued effects (I14): a queued effect whose authorization, snapshot, or producing context is invalidated holds or cancels before dispatch.
- Extends the replay runner with lifecycle controls (core/transport restart, clock advance, fault-at-checkpoint) and an acceptance harness: the crash-boundary battery at every named checkpoint, timer/main-step dedup (I16), gap/cursor recovery, sidecar continuity across core restart, and the three handoff stories end-to-end with identical observer traces on identical re-runs.

## Capabilities

### New Capabilities

- `replay/dispatch`: pre-dispatch revalidation, supersession, release-limit reservation, retry/reconciliation rules, and dispatch-time invalidation for the synthetic slice.
- `replay/recovery`: restart fencing (epochs/generations), crash-boundary recovery at the named checkpoints, deduplicated acceptance of resumed work, and deterministic replay acceptance.

### Modified Capabilities

(none — whole-turn, storage, and discretion requirements are unchanged; dispatch composes with them)

## Impact

- Grows `agora/fake_sidecar.py` (revalidation window, retry rules), `agora/coordinator.py` (supersession in the rewrite loop), `agora/stores.py` (release-limit reservation over the existing M1 `core_release_reservations` ledger — no DDL change), `agora/runner.py` (lifecycle controls, acceptance harness), and the replay tests.
- The architecture contracts are owned by the v0.2 handoff §6.2, §7, §8 (dispatch invariants) and §10 items 6–7; this change implements them, it does not redesign them.
- No new runtime dependencies; no real transport (Discord arrives at M5 — no Discord resources are needed for this change).
