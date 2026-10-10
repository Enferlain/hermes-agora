# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), with the change of using daily dated entries instead of releases.

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
