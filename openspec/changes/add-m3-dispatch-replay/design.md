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

The lease transaction that already fences epoch/generation/expiry also re-reads the bound versions: the authorization (still current and ACTIVE), the snapshot (still current, ACTIVE, not INVALID), the effect's target (still the current revision for reactions), the audience (disclosure revision and visibility unchanged — see D3), and the capability (still ACTIVE). Any mismatch raises the typed hold (POLICY_CHANGED / STALE_CONTEXT / TARGET_CHANGED / AUDIENCE_CHANGED / CAPABILITY_INVALID) and the outbox row goes HELD, never DISPATCH_STARTED. A change that lands after DISPATCH_STARTED is explicitly too late for prevention: the world call proceeds and recovery is conservative (UNKNOWN handling), per §6.2. This keeps "local dispatch linearization" exactly where M1 put it and adds only reads inside the already-serialized window.

One schema note carried from the M2 section-4/5 review: `CommitAuthorization.policy_version_vector` currently carries the snapshot's selected-fragment refs, not the commitment/policy versions a REQUIRED pass was keyed to. M3's revalidation must bind the versions it actually re-checks — either repurpose that field to the assessment's policy vector for reviewed authorizations, or add the assessment's vector to what the dispatcher reads — decided when D1 is implemented, not silently left as fragments.

### D2 — Supersession is the only requeue path for a shared logical id

`submit_authorized` for proposal revision N+1 of a logical effect whose prior outbox row is not terminal-SENT first calls `cancel_or_supersede`; only an acknowledged CANCELLED allows the enqueue (I09). TOO_LATE (already DISPATCH_STARTED or beyond) refuses the requeue — the coordinator surfaces a typed CONFLICT/TOO_LATE outcome, and the earlier revision's receipt stands. Exact duplicate submits (same bytes/digest) return the current state; conflicting bytes for the same logical id + revision return CONFLICT (both already structural in M1 and now pinned by tests).

### D3 — Material audience change is defined, bounded, and testable

"Material" means exactly: the current AudienceSnapshot's `disclosure_context_revision` differs from the one bound into the authorization, or its visibility/unknown-readers flags flipped. Message growth, history visibility churn within the same class, and ordinary new chatter are NOT material — those dispatch. This gives AUDIENCE_CHANGED a deterministic predicate instead of a judgment call, matches the §6.2 "audience material change versus harmless public chatter" split, and stays auditable (the compared revisions land in the audit row).

### D4 — Release limits are a core-side check-and-reserve in one transaction (I15)

A `core_release_reservations` table (DDL addition; schema-version bump is acceptable — M3 owns a persistence change, unlike M2's explicit no-DDL non-goal) records spent allowances keyed by (account, destination, window, units) from the capability's fixture-seeded `rate_budget_limits`. The sidecar's reserve step checks and inserts the spend in ONE core transaction; a second concurrent reservation for the same window cannot insert (unique key) and its effect stays HELD/QUEUED for a later window. Limits are fixture-fixed; nothing derives or widens them at runtime.

### D5 — Retry and reconciliation follow §6.2 exactly

FAILED_CONFIRMED may retry once, only with fake-world evidence of no effect plus still-valid policy/dependencies, under a fresh attempt id with the logical effect id preserved. UNKNOWN never retries: `reconcile` returns APPLIED → SENT, CERTIFIED_NOT_APPLIED → FAILED_CONFIRMED, INDETERMINATE → stays UNKNOWN with bounded retention. Cancellation is idempotent across the DURING_CANCEL checkpoint.

### D6 — Acceptance harness = runner lifecycle controls + crash-boundary battery + trace equality

`ReplayRunner` grows explicit controls — `restart_core()`, `restart_transport()`, `advance_clock(ms)`, and `fault_at(checkpoint)` — so a manifest's fault plan is a first-class fixture input. The acceptance battery drives every named checkpoint (AFTER_INGEST_BEFORE_ACK … DURING_CORRECTION_JOB), asserts the conservative recovery state after each, asserts DISPATCH_STARTED-at-restart becomes UNKNOWN even when the world could have proven no-send (query first, never guess), and compares full observer traces across two identical runs for byte equality. The I01–I16 matrix is recorded as a test-to-invariant mapping comment in the acceptance module; invariants already covered by M1/M2 tests are cited, not duplicated.

## Risks / Trade-offs

- **The rewrite-loop + supersession interaction is the highest-risk seam**: M2's bounded revision loop now must cancel-then-requeue under every hold path; tests cover the TOO_LATE and crash-between-cancel-and-requeue windows specifically.
- **The DDL addition** means older fixture databases cannot reopen across the change; acceptable inside a synthetic slice whose stores are created per run, and called out in the CHANGELOG.
- **Revalidation reads inside the serialized window** add latency to dispatch; in the fake slice this is irrelevant and the correctness gain (prevention before linearization) is the point of the milestone.
- **Trace byte-equality** could over-constrain tests to serialization trivia; the harness compares the observer trace (world rows + typed outcome summaries), not raw SQL dumps.
