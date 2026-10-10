# Design: add-m1-replay-turn

## Context

See proposal.md for motivation. The owning contracts are docs/architecture/v0.2 (addendum, then implementation handoff §2/§3/§4/§6.2/§7/§10); ROADMAP.md M1 sequences them. This design was produced by a three-designer panel (minimal-exact, test-platform, evolution-safe lenses) with adversarial judging; the designs converged on every load-bearing choice, and this document records the synthesized result plus the explicitly flagged deviations.

Constraints: Python 3.14, stdlib only; flat `agora/` package (location reconciled in docs/implementation-notes/2026-10-09-m0-foundation.md); module ownership per handoff §2 (contracts = records/validation/canonical binding, no policy/DB/model; stores = schemas/transactions/version checks/recovery, no prompts/effects); determinism everywhere (no wall-clock reads in runtime code).

## Goals / Non-Goals

**Goals:**

- Full handoff §4 record fidelity in the record layer — all 36 listed record types plus `ReviewEnvelope` (a §4.4 contract the task list omitted; the handoff letter wins).
- Two-domain SQLite persistence with exactly the §7 tables, crash-safe ingestion, virtual time, durable epochs/generations, and the eight named fault checkpoints.
- Behavioral narrowing happens only at operation level (what M1 wires); record shape is never narrowed.
- Fault injection so cheap that every M2/M3 flow adds one `trip()` line and every fault test is a two-line arrange step.

**Non-Goals:**

- No coordinator/context-memory/commit-gate/fake-sidecar logic beyond what ingestion needs (next task: whole-turn wiring).
- No retention expiry jobs (retention fields exist; the jobs are M2/M3).
- No review routing, access evaluation, transforms, or dispatch state machines (M2/M3 define semantics; the spine stores their records).
- No model adapter, no live transport, no new dependencies.

## Decisions

### D1 — Record representation: frozen dataclasses + one generic codec

Every record is a `@dataclass(frozen=True, slots=True, kw_only=True)` inheriting one header base (`schema_version=2` constant, `id`, `revision`, `created_at` strict UTC RFC3339 `Z`). No schema DSL, no dynamic classes: static types stay `ty`-checkable and the field list is the handoff table transcribed. Serialization uses ONE generic codec driven by `dataclasses.fields` with cached per-class type plans (kinds: StrEnum, RecordRef, RuntimeOrigin, VersionVector, tuple-of-T, sorted Pair tuples, optional). Alternatives rejected: per-class hand-written to/from functions (3× the code, drift risk), declarative spec tables (a second source of truth for field sets).

### D2 — Validation: construction-time + closed parse boundary

`__post_init__` enforces the header (schema_version literal 2, `ID_PATTERN` 1–128 opaque charset, revision > 0, RFC3339) and dispatches to shared helpers (`check_sha256`, `check_bounded`, finite floats, `exactly_one_of`, sortedness). An invalid record cannot exist as an object. The storage/fixture boundary goes through `record_from_payload(cls, mapping)` with an exact key-set check — missing AND unknown keys fail with INVALID_SCHEMA. Model-facing parsing (`parse_model_step`) additionally rejects `MODEL_FORBIDDEN_KEYS` (authority/lease/epoch/coverage/verified-actor vocabulary), making a fabricated `CommitAuthorization` inside model output invalid by construction.

### D3 — Canonical binding

`canonical_json_bytes`: pre-walk rejects non-finite floats and lone surrogates (strict UTF-8 encode), then `json.dumps(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")`. Semantically unordered ID sets are sorted by the record before serialization (VersionVector, Pair tuples); ordered content tuples serialize in order; no Unicode normalization anywhere. `canonical_digest` = SHA-256 hexdigest of the canonical bytes. Rendered effect payloads use their exact §4.4 shapes and digest the encoded bytes.

### D4 — Conflicts resolved (addendum/handoff precedence applied explicitly)

1. **Release ledger in review context**: `ReviewEnvelope` is implemented as the handoff's closed 12-field set — no ledger field. The v0.1 reviewer-facing ledger is consciously not carried; the question is tracked as bead `hermes-agora-2qm` and the extension path is a typed field under a future schema_version bump. Not a silent drop.
2. **`MemoryAccessResult` (addendum D07 outranks the handoff table)**: extended with `permitted_uses`, `producer_coverage_ref`, `transform_certificate_ref`, `authorization_refs`, `inherited_commitment_refs`, `view_ref` — references only, never duplicated payload bytes.
3. **Required-assessment budget (addendum D03)**: the home is the persisted `ReviewAssessment` record itself — its record id deterministically embeds plan id + proposal revision, so re-acceptance is idempotent and "at most one assessment per submission" is structural via the records table's uniqueness, with `ReviewRequirement.budget` carrying the cap value. No new WorkItem field, no table beyond §7.

### D5 — Flagged local decisions (handoff unspecified, bounded and reversible)

- `MemoryAccessRequest` carries `necessity_text` XOR `necessity_payload_ref`; persistence swaps bounded text into `core.payloads` so necessity text never sits in immutable record JSON (§3 rule).
- `CapabilityEnvelope.rate/budget_limits` (unnamed shape) = bounded label-sorted `(label, value)` pairs.
- `DeliveryReceipt.state` vocabulary = `SENT/FAILED_CONFIRMED/UNKNOWN`, aligned with outbox terminal states.
- `UseAuthorization`'s tabulated `revision` is the common header revision only — no duplicate field.
- GAP encoding = explicit `recovery='GAP'` marker rows in `transport.events` (§7 allows gap records) plus a core ingestion row and cursor resync.
- `core.ingestion` gains `UNIQUE(consumer_id, journal_seq)` beyond the §7-stated key so a same-seq/different-content replay is a detected conflict.
- `WorkItem`, `DispatchLease`, `DisclosureReservation` live only in their dedicated §7 tables (columns mirroring record fields) — no dual storage in `core.records`.
- `ingest_batch` orchestration lives in `stores.py` for the spine (transaction mechanics + injected pure `WorkPlanner` keeping stores policy-free); the coordinator takes orchestration ownership in the wiring task.
- The transport store class is named `TransportStore`, not the handoff §2 table's `FakeTransportStore`: §2 permits repository naming when boundaries and responsibilities are preserved, and the store owns real recovery semantics that will outlive the fake transport. Boundaries unchanged.
- Known gap carried to wiring/M2 (beads hermes-agora-4za): §7's "typed refs" core.records constraint has no store-level target-type verification yet; the spine validates ref shape only.

### D6 — Stores: two domains, exact §7 tables, explicit transactions

`sqlite3` with `isolation_level=None`, WAL, `synchronous=FULL`, `busy_timeout`; a `transaction()` context manager issues `BEGIN IMMEDIATE`/COMMIT/ROLLBACK — `InjectedCrash` propagates through it (rollback = simulated process death; tests reopen and continue, which IS the restart path). Stores never read clocks — all times are parameters (`now_ms`, `created_at`). DDL is string constants; open writes `schema_version` + DDL digest to meta and refuses unknown or older versions (`SchemaVersionError`). `CoreStore.open` bumps durable `runtime_epoch`; `TransportStore.open` bumps `transport_generation`; version-checked operations reject old-epoch records with STALE_EPOCH. `core.records` keys `(record_id, revision)` with a partial unique current-revision flag — second revisions append and flip `is_current` in one transaction; rewriting an existing row fails CONFLICT. Dependencies insertion checks acyclicity (recursive CTE) and fails CONFLICT. Audit rows accept only opaque refs + registry codes. The fake world's evidence store is a third fixture DB (M1 wiring task builds it; core reads it only via the reconciliation port).

### D7 — Ports: clock, checkpoints, interfaces

`VirtualClock(anchor_rfc3339, start_ms=0)`: `now_ms()` monotonic virtual ms; `advance_ms` with negative delta → error (clock never moves backwards within a run); `now_rfc3339()` = anchor + elapsed ms (display time is a pure function, never a wall read); `persist`/`resume` through store meta. `CrashCheckpoint` StrEnum with exactly the eight handoff names; `FaultInjector.arm/disarm/trip` one-shot, `InjectedCrash(checkpoint)` simulating process death. `MainModelPort`/`ReviewerPort`/`PerceptionPort` defined as `Protocol`s now (interfaces per §5; scripted implementations arrive with wiring).

### D8 — Test plan (handoff §10 order item 1 + spine acceptance)

- `tests/test_contracts_records.py` — header bounds/defaults, unknown fields/enums, RecordRef/VersionVector rules, canonical binding (set-order equivalence, NaN rejection, surrogate rejection, ordered-content preservation), runtime-only key rejection, reason-code closure, event-kind closure, model-step parsing incl. forbidden keys.
- `tests/test_stores_core.py` — append-only revisions, current flip, in-place edit CONFLICT, unknown-schema refusal, epoch bump + STALE_EPOCH, payload purge semantics, dependency acyclicity, audit safety, work trigger-key dedup.
- `tests/test_stores_ingestion.py` — happy-path batch, crash-before-ack (arm checkpoint → reopen → no duplicate work, cursor intact, retried batch = duplicates), out-of-order no-advance, GAP emission and resync.
- `tests/test_ports_clock.py` — monotonicity, negative advance, display-time determinism, persist/resume, injector one-shot semantics.
- Shared `assert_store_invariants()` helper for reuse by M2/M3 suites.

## Risks / Trade-offs

- **contracts.py is large (~1200+ lines) by contract, not by accident**: the §4 record set is the law; splitting files would obscure it. Parity between dataclass fields and codec plans is guarded by a round-trip test over every record type.
- **SQLite "crash" simulation is transaction-rollback + reopen, not process kill**: it exercises the same durable-state surface (WAL + FULL sync). True process-kill tests can be added later without design change.
- **Two clocks (virtual ms vs display UTC)** invite bugs where display time leaks into lease math; the design keeps leases/budgets/retention strictly on `now_ms` and tests assert display time never moves independently.
- **The assessment-cap home (D4.3) is interpretive**: if a later review wants a WorkItem-level counter instead, it is an additive change, not a rewrite.
- **Open question (bead hermes-agora-2qm)**: reviewer release-ledger visibility is unresolved architecture-wide; M1 does not depend on the answer, M2 review routing does.
- **Judged design**: the panel's two adversarial judges split between the minimalist and §7-maximalist designs; the synthesis took the winner's structure plus grafted fixes (work restart recovery on open, hashed bounded work ids, strict duplicate-key-rejecting JSON loads, FAILED_CONFIRMED→QUEUED single retry path, shared store invariant battery, non-GRANTED access results carrying no permissions). Fatal flaws the judges found in the designs were verified absent from the implementation before commit.
