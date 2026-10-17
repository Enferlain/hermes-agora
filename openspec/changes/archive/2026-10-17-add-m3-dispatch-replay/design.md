# Design: add-m3-dispatch-replay

## Context

See proposal.md. The dispatch semantics are pinned by the v0.2 handoff (§5 sidecar/lease rows, §6.2 outbox and lease state machines, §8 invariants I08–I16, §10 test items 6–7) — this design covers only the genuinely open implementation choices. The M1 skeleton already persists PREPARED attempts, fenced leases (epoch/generation/expiry), durable DISPATCH_STARTED as the local linearization point, UNKNOWN-only-through-reconciliation, and cancel hooks; the stores already reopen with epoch/generation bumps and gap markers. M3 completes and exercises the machinery; it does not rebuild it.

## Goals / Non-Goals

**Goals:**

- Make dispatch-time prevention real: revalidation happens inside the reserve/consume window, before DISPATCH_STARTED, with typed reason codes.
- Close the r1/r2 hole the M2 rewrite loop opened: revision N+1 shares the logical effect id, so requeueing must go through acknowledged cancellation.
- Cover every named crash checkpoint and the I01–I16 matrix with focused deterministic tests, and run the three handoff stories end-to-end including restarts.
- Keep all fixture-driven determinism: same manifest + fault plan → byte-identical observer traces.

**Non-Goals:**

- No real transport, no cross-DB atomicity claims, no exactly-once delivery claim (fake-world preconditions only).
- No condition-language schedules (fixed-time timers only, unchanged).
- No numeric privacy budget, no erasure semantics, no Clef (unchanged §11 deferrals).
- No new authority semantics — discretion evaluation is settled by M2.

## Decisions

### D1 — Revalidation lives in the reserve/consume window, before DISPATCH_STARTED

The lease transaction that already fences epoch/generation/expiry also re-reads the bound versions: the authorization (still current and ACTIVE), the policy/commitment revisions its policy vector binds (see the binding below — everything routing consulted, regardless of route), the snapshot (still current, ACTIVE, not INVALID), the effect's target (still the current revision for reactions), the audience (disclosure revision and visibility unchanged — see D3), and the capability (still ACTIVE). Any mismatch raises the typed hold (POLICY_CHANGED / STALE_CONTEXT / TARGET_CHANGED / AUDIENCE_CHANGED / CAPABILITY_INVALID) and the outbox row goes HELD, never DISPATCH_STARTED. A change that lands after DISPATCH_STARTED is explicitly too late for prevention: the world call proceeds and recovery is conservative (UNKNOWN handling), per §6.2. This keeps "local dispatch linearization" exactly where M1 put it and adds only reads inside the already-serialized window.

One binding is settled now, carried from the M2 section-4/5 review: `CommitAuthorization.policy_version_vector` means what its name says — the complete set of policy/commitment revisions routing consulted for this plan, not merely the subset that ended up restricting it. Routing result ≠ routing evidence: NONE and ADVISORY are verdicts derived from state (a commitment exists but allows outward use ⇒ NONE), and that state can go stale between authorization and dispatch, so the evidence must be bound too. Concretely, the field binds every commitment the routing walk reads — the snapshot's inherited commitments (with its active views'), the producer coverage's fragment, view and policy commitments (the policy path at the revision routing resolved), and the derived intents' goal-fragment commitments — each at the revision routing actually evaluated, plus those commitments' ResourcePolicy/UseAuthorization evidence refs (a policy revision can flip a commitment's meaning without bumping the commitment record, so commitments alone are not enough). The set is plan-anchored and finite — exactly the records the walk already loads, never the whole policy database — and an empty vector means routing consulted nothing, not that nothing restricted. For a REQUIRED authorization, the `ReviewRequirement`/`ReviewAssessment` vector is precisely the restriction subset of this consulted vector, at matching revisions, by construction (the gate builds both from the same walk at the same instant). The gate stops writing the snapshot's fragment version vector into the field (agora/commit_gate.py does that today); the snapshot's own vector stays on the snapshot record, and snapshot freshness remains its own revalidation input per the paragraph above. M3 landed both sides: routing surfaces its inputs (the consulted set, not just its verdict) as the authorization's field, and the dispatcher's revalidation reads exactly that field with a per-ref revision comparison identical to the gate's §8.5 recheck. The deliberate trade: a revision to any consulted commitment or policy, even one that stays permissive, holds the effect for a re-route through the rewrite loop — conservative by design, and deterministic in the slice where revisions are scripted.

### D2 — Supersession is the only requeue path for a shared logical id

`submit_authorized` for proposal revision N+1 of a logical effect whose prior outbox row is not terminal-SENT first calls `cancel_or_supersede`; only an acknowledged CANCELLED allows the enqueue (I09). TOO_LATE (already DISPATCH_STARTED or beyond) refuses the requeue — the coordinator surfaces a typed CONFLICT/TOO_LATE outcome, and the earlier revision's receipt stands. Exact duplicate submits (same bytes/digest) return the current state; conflicting bytes for the same logical id + revision return typed CONFLICT — the authorization id keys by logical id and revision so the gate refuses a re-authorization of different bytes, the sidecar's digest and durable-bytes binding refuses the direct route, and the transport's unique-key row refuses the rest; all three pinned by tests. Cancelling a FAILED_CONFIRMED revision acknowledges the supersession but never refunds its window slot — the accounting unit is the logical effect, and its replacement rides the same spent reservation.

### D3 — Material audience change is defined, bounded, and testable

"Material" means exactly: the current AudienceSnapshot's `disclosure_context_revision` differs from the one bound into the authorization, or its visibility/unknown-readers flags flipped. Message growth, history visibility churn within the same class, and ordinary new chatter are NOT material — those dispatch. This gives AUDIENCE_CHANGED a deterministic predicate instead of a judgment call, matches the §6.2 "audience material change versus harmless public chatter" split, and stays auditable (the compared revisions land in the audit row).

### D4 — Release limits are a per-effect ledger SUM under BEGIN IMMEDIATE, on the M1 table (I15)

`core_release_reservations` is not a new table — it has existed since M1 (handoff §5: reservation_id, effect_id, constraint_group_id, query_key, state, accounting_units, UNIQUE(effect_id, constraint_group_id)) and is so far DDL-only. M3 spends it as-is, with no DDL change. Each reservation is one row per effect; the window is encoded in `query_key` (destination + constraint + window start, derived from the virtual clock and the fixture-fixed window size of the capability's seeded `rate_budget_limits`, e.g. `per_minute` → 60 s windows). The reserve step opens ONE core transaction with BEGIN IMMEDIATE — the store's existing write-serialization mode — and inside it computes SUM(accounting_units) over rows sharing the constraint_group_id and window query_key in spending states (RESERVED, COMMITTED, POSSIBLY_COMMITTED; CANCELLED_BEFORE_DISPATCH no longer counts), then inserts this effect's row only if the sum stays within the fixture limit. Over budget inserts nothing and the effect stays HELD/QUEUED with RELEASE_LIMIT for a later window. Concurrency is settled by the write lock, not by a unique window row: two competing effects cannot both observe the pre-spend sum under BEGIN IMMEDIATE, and `per_minute = 6` correctly admits six one-unit reservations per window — a unique (account, destination, window) row would collapse every limit to 1. Settlement follows the addendum's state machine (RESERVED → COMMITTED/POSSIBLY_COMMITTED on dispatch outcomes; CANCELLED_BEFORE_DISPATCH only via acknowledged cancellation before DISPATCH_STARTED); UNKNOWN and observed partial effects never auto-refund. Limits are fixture-fixed; nothing derives or widens them at runtime.

### D5 — Retry and reconciliation follow §6.2 exactly

FAILED_CONFIRMED may retry once, only with fake-world evidence of no effect plus still-valid policy/dependencies, under a fresh attempt id with the logical effect id preserved. UNKNOWN never retries: `reconcile` returns APPLIED → SENT, CERTIFIED_NOT_APPLIED → FAILED_CONFIRMED, INDETERMINATE → stays UNKNOWN with bounded retention. Cancellation is idempotent across the DURING_CANCEL checkpoint.

### D6 — Acceptance harness = runner lifecycle controls + crash-boundary battery + trace equality

`ReplayRunner` grows explicit controls — `restart_core()`, `restart_transport()`, `advance_clock(ms)`, and `fault_at(checkpoint)` — so a manifest's fault plan is a first-class fixture input. The acceptance battery drives every named checkpoint (AFTER_INGEST_BEFORE_ACK … DURING_CORRECTION_JOB), asserts the conservative recovery state after each, asserts DISPATCH_STARTED-at-restart becomes UNKNOWN even when the world could have proven no-send (query first, never guess), and compares full observer traces across two identical runs for byte equality. The I01–I16 matrix is recorded as a test-to-invariant mapping comment in the acceptance module; invariants already covered by M1/M2 tests are cited, not duplicated.

## Risks / Trade-offs

- **The rewrite-loop + supersession interaction is the highest-risk seam**: M2's bounded revision loop now must cancel-then-requeue under every hold path; tests cover the TOO_LATE and crash-between-cancel-and-requeue windows specifically.
- **No DDL change remains in this design**: release limits reuse the M1 `core_release_reservations` ledger as-is. If implementation discovers it needs a column after all (e.g. an explicit window column instead of the query_key encoding), that is a schema-version bump decided and called out then, not assumed now.
- **Revalidation reads inside the serialized window** add latency to dispatch; in the fake slice this is irrelevant and the correctness gain (prevention before linearization) is the point of the milestone.
- **Trace byte-equality** could over-constrain tests to serialization trivia; the harness compares the observer trace (world rows + typed outcome summaries), not raw SQL dumps.
