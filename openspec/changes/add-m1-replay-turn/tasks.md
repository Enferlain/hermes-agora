# Tasks: add-m1-replay-turn

## 1. Contract layer (`agora/contracts.py`)

- [x] 1.1 Header base, value types, and registries: `Record` base (schema_version=2, id/revision/created_at validation), `RecordRef`, `VersionVector`, `RuntimeOrigin`, all closed StrEnums (event kinds, states, routes, access/view kinds, assertion statuses, source kinds), the `ReasonCode` registry. Verify: `uv run pytest tests/test_contracts_records.py -k "header or ref or reason"` passes.
- [x] 1.2 Canonical binding: `canonical_json_bytes` (sorted keys, no whitespace, finite-only, surrogate-rejecting), `canonical_digest`, rendered SEND_MESSAGE / ADD_REACTION payload shapes per handoff §4.4. Verify: canonical-binding tests (set-order equivalence, NaN, surrogates, ordered content) pass.
- [x] 1.3 All 36 §4 record types + `ReviewEnvelope` as frozen dataclasses with per-type validation and the D4/D5 field decisions (widened `MemoryAccessResult`, `necessity_text` XOR `necessity_payload_ref`, Pair-tuple limits, `DeliveryState`). Verify: unknown-field/unknown-enum/invalid-ref tests pass for representative record families.
- [x] 1.4 Generic codec (`record_to_payload` / `record_from_payload`) with exact key-set checks, plus `parse_model_step` with `MODEL_FORBIDDEN_KEYS` rejection. Verify: round-trip test over every record type passes; fabricated-authorization test passes.

## 2. Storage layer (`agora/stores.py`)

- [x] 2.1 Connection/transaction machinery (WAL, FULL sync, `BEGIN IMMEDIATE` context manager surviving `InjectedCrash`), DDL constants for the exact §7 tables, schema-version-checked open that refuses unknown versions, durable epoch/generation bumps on open. Verify: schema-refusal and epoch tests pass.
- [x] 2.2 Versioned record store: append-only `(record_id, revision)` with current-revision flip in one transaction, `CONFLICT` on in-place edits and duplicate current revisions; out-of-line payload store with `ACTIVE/PURGED` and expiry fields; acyclic dependency insertion; audit writes restricted to opaque refs + registry codes; work table with unique trigger keys. Verify: `tests/test_stores_core.py` passes.
- [x] 2.3 Ingestion: `build_batch` from the transport journal, `ingest_batch` one-transaction core commit (event refs + deduped work + contiguous cursor) before transport ack, duplicate detection, out-of-order no-advance, GAP marker emission and cursor resync, `AFTER_INGEST_BEFORE_ACK` checkpoint trip. Verify: `tests/test_stores_ingestion.py` passes including the crash-before-ack restart scenario.

## 3. Ports layer (`agora/ports.py`)

- [x] 3.1 `VirtualClock` (monotonic virtual ms anchored to manifest origin, derived display RFC3339, persist/resume via store meta), `CrashCheckpoint` enum + `FaultInjector` (arm/disarm/one-shot trip), and the `MainModelPort`/`ReviewerPort`/`PerceptionPort` Protocol interfaces. Verify: `tests/test_ports_clock.py` passes.

## 4. Spine closure

- [ ] 4.1 Full gates green (`uv run pytest`, `uv run ruff check .`, `uv run ruff format --check agora tests`, `uv run ty check`), CHANGELOG dated entry, `bd close` the spine task, and a review pass by a review agent with findings addressed. Verify: all commands exit 0 and the review findings log is empty or addressed.

## 5. Whole-turn wiring (next task: `hermes-agora-043.2`)

- [ ] 5.1 Scripted ports + coordinator + context/memory stub-with-real-records: event→work→episode turn with budgets from the run manifest. Verify: deterministic ingest-to-decision trace for one event.
- [ ] 5.2 Ordinary CommitGate routing (NONE path + UNSUPPORTED denials) and fake sidecar/world with durable visibility evidence. Verify: speak trace and unsupported-effect denial tests pass.
- [ ] 5.3 Four outcome demonstrations (message, reaction, explicit no action, timer-driven initiation) via a replay CLI command; restart-continuation smoke. Verify: M1 exit evidence — expected observer trace and durable state from a deterministic replay command.
