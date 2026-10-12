# Decision: release-ledger visibility in REQUIRED review context

12 October 2026 · Resolves beads hermes-agora-2qm

## Question

v0.1 runtime-contracts (line 246) named the "recent release ledger" as a semantic-review context component so a reviewer sees cumulative disclosure history. The v0.2 handoff's `ReviewEnvelope` (§4.4) is a closed 12-field set with no release-ledger field, and the v0.2 addendum (D07) moves release accounting into atomic `core.release_reservations` without explicitly superseding the reviewer-facing ledger. As literally specified, the M2 REQUIRED reviewer decides semantic compliance with zero cumulative-release visibility.

## Decision

For M2, `ReviewEnvelope` stays exactly the handoff's closed field set. The omission is confirmed as deliberate slice scoping, not an oversight.

Rationale:

1. Document precedence (addendum → handoff → v0.1 for unaffected matters) puts the handoff's envelope ahead of the v0.1 reviewer-context provision, and nothing in the addendum restores it.
2. The information is not lost to the runtime: cumulative release accounting lives in `core.release_reservations` and is available to routing and limit enforcement, which is where D07 places it.
3. The M2 reviewer is scripted; a ledger field would be inert this milestone anyway.

## Extension path

If M3 acceptance stories or M4 model-backed experiments show that REQUIRED reviewers fail without cumulative-disclosure context, the extension is explicit: a new typed field (e.g. `release_ledger_summary_refs`) under a `schema_version` bump, with routing-rule changes and tests — a visible contract revision, not accretion. Until then, no reviewer-facing ledger is implemented, and this note records that consciously.
