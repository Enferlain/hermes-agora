# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), with the change of using daily dated entries instead of releases.

## 2026-10-12

### Added

- Manifest registries (manifest version 2, M2 task 1.2): a `registries` section seeds principals, protected/ordinary resources, commitments, policies, authority bindings, use authorizations, and fixture-fixed transform parameters through the trusted fixture port at bootstrap — one typed-ref-verified batch, all-or-nothing. Unknown registry ids fail startup before anything is written; reopens never re-seed. Fixture transform parameters persist as core config meta, and transform ids/parameters validate against the fixed two-transform registry.
- `agora/transforms.py`: the fixed transform registry (`FIXED_FIELD_VIEW`, `FIXED_AFFORDABILITY_PREDICATE`) as pure functions over an authorized payload with manifest-fixed parameters — no arbitrary functions or dynamic registry edits.
- A five-resource-class fixture builder (public project fact, agent-private ordinary preference, human financial protected fact, third-party confidential disclosure with no agent authorization, joint ALL_LISTED protected record) shared by the M2 evaluation/routing/story tests.

- Store-level typed-reference verification (handoff §7 core.records "typed refs", M2 task 1.1 / beads hermes-agora-4za): a hand-authored `REF_TARGETS` map assigns every RecordRef-bearing field its expected target record type(s), verified inside the insert transaction — a missing target, wrong target type, or unresolvable cited revision fails `INVALID_REFERENCE` with nothing written. A codec-derived drift test keeps the map aligned with the actual record fields, and refs nested in Provenance/RuntimeOrigin values are verified too.
- `CoreStore.put_records` batch insert: one transaction with reference verification after every insert, so fixture loads and view/certificate pairs can carry intra-batch forward and circular references (any failure rolls back the whole batch); `CoreStore.verify_reference` for coordinator-side pre-validation of model-supplied ids.
- The coordinator now persists the §4.2 `WorkItem` record once per turn (lazily under the resolved intent, never rewritten across crash replay), pre-validates model-supplied effect targets and declared facts before writing anything (a bad reference is a typed HELD denial with `INVALID_REFERENCE`, not a crash), and `TurnOutcome.effect_refs` cites Effect records only.

### Changed

- Updated README and ROADMAP to the implemented state: M0/M1 complete, the replay slice and its module layout described as-is, M2 named the active milestone.
- Archived the completed `add-m1-replay-turn` OpenSpec change and synced its three capability specs into the main specs (`openspec/specs/replay/{record-contracts,storage,whole-turn}`).

### Added

- Recorded the release-ledger review-visibility decision (implementation note): the M2 ReviewEnvelope remains the handoff's closed field set; reviewer-facing ledger visibility is a documented schema-bump extension, not a silent drop.

### Fixed

- Replaced the coordinator's reach into `CoreStore._conn` with a public `ready_work_ids()` store method and restored concrete `ContextSnapshot` typing in the plan-submission path (beads msp/bbx).

## 2026-10-11

### Added

- Promoted `agora/contracts.py` (~2,600 lines) to the `agora/contracts/` package (errors, enums, values, canonical, records, codec, model_steps) per the structure convention; the `from agora.contracts import X` surface is unchanged and all 110 tests pass without a single test-import edit.
- Added the package structure convention (implementation note): handoff §2 boundary names are permanent top-level modules; modules promote to re-exporting packages on a growth trigger; contracts/ is the first planned promotion at M2 start.

- Implemented the M1 whole-turn wiring: coordinator turn loop with manifest budgets, compare-and-set step acceptance, and the crash checkpoint between model and acceptance; context/memory snapshot assembly with CONTEXT_ASSEMBLY and MAIN producer coverage; CommitGate with code checks, UNSUPPORTED/HARD_DENY determinations, and versioned CommitAuthorization; fake sidecar with PREPARED attempts, single-use epoch/generation/expiry-fenced leases, durable DISPATCH_STARTED linearization, world dispatch, conservative UNKNOWN, and reconciliation; fake world as independent durable visibility evidence; scripted main model port; replay runner + `agora replay` CLI with validated run manifests parsed through the model boundary.
- Demonstrated the four M1 outcomes deterministically: ordinary message, reaction (exact rendered shape), explicit no action (no outbox/attempt/world rows), and timer-driven voluntary initiation (deduped fixed-time defer wake); plus unsupported-destination denial staying inert, quiet budget exhaustion, and crash-recovery continuation.

### Fixed

- Independent GLM-flash review findings, each with a regression test: ingestion no longer wedges on revised source events (dedup keyed by event id and revision); exact-duplicate effect submission returns current state instead of conflicting; runtime-only field rejection now descends into tuples; model steps reject missing required fields instead of defaulting them; audit reason codes are registry-checked; the virtual clock rejects negative origins and non-positive deltas; a crash during first-time store creation is recoverable instead of bricking the store.

## 2026-10-10

### Added

- Implemented the M1 contract and storage spine: all 35 handoff §4 record types plus `ReviewEnvelope` as validated frozen records with strict field-set and closed-enum validation, canonical payload binding (sorted-key UTF-8 JSON, SHA-256 digests, finite-only, surrogate-rejecting), runtime-only field discipline on the model-step boundary, and the closed 33-code reason registry (`agora/contracts.py`).
- Implemented the two SQLite ownership domains (`agora/stores.py`): the exact §7 tables with schema-digest-checked startup that refuses unknown versions, append-only versioned records with a single current revision, out-of-line payloads with virtual-time purge, acyclic producer dependencies, trigger-key-deduplicated work with §6.1 restart recovery, durable runtime epoch and transport generation bumps on restart, basic outbox/attempt transitions including acknowledged supersession and the single FAILED_CONFIRMED retry path, and a shared store invariant battery.
- Implemented crash-safe ingestion: event refs, deduped work, and the contiguous cursor commit in one core transaction before the transport acknowledgement; deleted retained journal entries become explicit GAP markers; contiguous journal assignment prevents hole-punching (`ingest_batch`).
- Implemented the virtual monotonic clock (display UTC derived from the manifest anchor, persist/resume via store meta), the eight named crash checkpoints with one-shot fault injection, and the main/review/perception port interfaces (`agora/ports.py`).
- Added 97 deterministic tests covering handoff §10 order item 1 (types and records), canonical binding, runtime-only rejection, ingestion crash-before-ack restart, GAP semantics, epoch fencing, out-of-line necessity-text storage, and clock/injector determinism.
- Opened the OpenSpec change `add-m1-replay-turn` (proposal, three capability specs, judged design synthesis, task list) covering the M1 milestone.
- Enforced STALE_EPOCH fencing on version-checked record writes and fixed the out-of-line text swap to target the record's own payload-ref field (final review findings).

## 2026-10-09

### Added

- Added a uv-managed Python project skeleton (`agora` package, Python 3.14) with a version-only CLI entry point and pytest/ruff/ty quality gates.
- Added the architecture documentation index defining revision precedence, and implementation notes recording the M0 implementation-location, Python-version, and workflow decisions.
- Laid the roadmap milestones and near-term implementation packages into Beads as dependency-chained epics and tasks.

### Changed

- Aligned ruff, pytest, and ty configuration with the shared data-processing toolchain conventions: expanded lint rule set (`E,F,I,UP,B,SIM,C4,TID252,RUF`), line length 100, quiet test output, bounded dev-tool versions, explicit ty Python pin.

### Fixed

### Removed
