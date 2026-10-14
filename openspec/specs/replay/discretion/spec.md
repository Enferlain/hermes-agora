## Purpose

Contextual discretion for the replay slice: who may know what, in which precision, for which use — and which proposals need semantic review before committing. Implements the access/use distinction, protected views, scoped memory, and selective review routing.

## Requirements

### Requirement: Domain-scoped authority evaluation

Access evaluation matches AuthorityBindings, ResourcePolicy approval rules (SINGLE_ISSUER: one active eligible issuer suffices; ALL_LISTED: all configured issuers), and UseAuthorizations covering operation, data class, maximum precision, allowed transforms, permitted purpose classes, and audience class bounds. READ/TRANSFORM/REVIEW/STORE require RESOURCE_USE bindings; EXPRESS/OBSERVABLE_INFLUENCE require DISCLOSURE bindings. No principal kind, ownership, custody, subject count, or purpose rationale upgrades authority.

#### Scenario: Missing issuer authority

- **WHEN** a protected read is requested and no matching active binding exists for any eligible issuer
- **THEN** the result is PENDING_AUTHORITY with view NONE and no value

#### Scenario: Conflicting joint rules

- **WHEN** contradictory active commitments or policies both apply to the request
- **THEN** the result is CONFLICT, not an arbitrary winner

#### Scenario: Operator is not a universal key

- **WHEN** a HUMAN_OPERATOR principal has no matching binding for a third-party confidential resource
- **THEN** the evaluation denies exactly as it would for any other principal

### Requirement: Access status, view shape, and use are independent

GRANTED results carry one of FULL/PARTIAL/ABSTRACTED with an active view reference, independent use permissions, producer coverage, transform/policy basis, and inherited commitments; every non-GRANTED result carries view NONE with no value and no permitted uses. Necessity text is stored out-of-line, never in record JSON or audit rows.

#### Scenario: Granted partial view

- **WHEN** a FIELDS-precision authorization covers the requested purpose class and audience bound
- **THEN** the result is GRANTED with view_kind PARTIAL, a view citing its authorization refs, and permitted uses exactly matching the authorization

#### Scenario: Denied carries nothing

- **WHEN** the result is DENIED, PENDING_AUTHORITY, UNAVAILABLE, or CONFLICT
- **THEN** view_kind is NONE, no view reference, no value, and no permitted uses

### Requirement: Fixed transform registry

Only FIXED_FIELD_VIEW and FIXED_AFFORDABILITY_PREDICATE exist, identified by registry id with fixture-fixed parameters. A transform certificate discharges only its explicitly listed commitments; all other inherited commitments survive the view.

#### Scenario: Affordability predicate

- **WHEN** the affordability transform runs over a protected financial view with fixture-fixed thresholds
- **THEN** the output view exposes the predicate result only, cites its certificate, and retains every commitment the certificate does not discharge

#### Scenario: Unregistered transform

- **WHEN** a request names a transform id absent from the registry
- **THEN** the request fails; no dynamic registry edits and no arbitrary functions exist

### Requirement: Scoped memory lifecycle

Memory eligibility is computed from stage, scope kind/ref, status, and expiry per scope rules; promotion EPHEMERAL → SOCIAL/CANDIDATE → DURABLE is explicit and never widens scope (no BROAD inference from DURABLE); corrections supersede the assertion and invalidate affected snapshots and late derivations whose captured input version vector no longer matches; episode membership or room merges confer no scope rights; no global retrieval index exists in this slice.

#### Scenario: Promotion without widening

- **WHEN** a CANDIDATE memory in room scope is promoted to DURABLE
- **THEN** its scope remains the source room; ordinary private memories remain discretion-eligible in other rooms

#### Scenario: Stale summary after correction

- **WHEN** a source assertion is corrected after a summary was derived from it
- **THEN** the summary's derivation becomes stale, its payload is ineligible for context, and affected snapshots are invalidated

#### Scenario: Strong internal-only containment

- **WHEN** a strongly INTERNAL_ONLY view would enter an externally effect-capable snapshot (including a DM)
- **THEN** admission fails structurally; it is never a reviewer-waivable exception

### Requirement: Selective review routing

The commit gate routes every plan: HARD_DENY for invalid capability/sink/view-admission/version/authority or explicitly forbidden protected operations; REQUIRED when any active STRONG commitment in the snapshot, plan producer inputs, or derived intent needs expression or observable-use resolution (relevant QUIET/DEFER included); ADVISORY for fixture-configured ordinary concerns; NONE otherwise. Sensor scores alone cannot create REQUIRED. A disclosed-but-unclaimed dependency is included via runtime coverage.

#### Scenario: Ordinary speech needs no inference

- **WHEN** an ordinary public message plan with no STRONG commitments is evaluated
- **THEN** the route is NONE and no reviewer call is made

#### Scenario: Strong restriction forces review

- **WHEN** a plan's context includes an active STRONG expression restriction on its derived inputs
- **THEN** the route is REQUIRED and dispatch waits on a passing, version-matched assessment

#### Scenario: Advisory can be declined

- **WHEN** an ADVISORY review records concerns on an ordinary plan
- **THEN** the recorded advice does not block delivery

### Requirement: Required assessment protocol

REQUIRED builds the closed ReviewEnvelope (exact proposal digest, audience/intent refs, immutable relevant policies and commitments, authorized comparison views, bounded quote context, runtime-assigned coverage); PASS is keyed to the proposal digest and current policy/snapshot versions; CONCERNS, INSUFFICIENT_CONTEXT, missing comparison views, or reviewer timeout yield HOLD; a failed required assessment cannot be downgraded on resubmission — each rewrite is a new revision that is fully re-routed.

#### Scenario: Facts missing holds the proposal

- **WHEN** a fact-dependent restriction has no authorized comparison view
- **THEN** the proposal is HELD, not evaluated on descriptors alone

#### Scenario: Version change voids a pass

- **WHEN** a relevant policy revision lands after the assessment but before dispatch
- **THEN** the assessment no longer authorizes the current bytes

### Requirement: Typed references verified at insert

Record insertion verifies each RecordRef field's target record type against the record's reference map; refs pointing at the wrong type or at missing records fail with INVALID_REFERENCE in the same transaction.

#### Scenario: Wrong target type

- **WHEN** a record's field expects an Intent reference but cites a Principal id
- **THEN** the insert fails with INVALID_REFERENCE and nothing is written
