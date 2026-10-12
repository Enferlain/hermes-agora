## Purpose

Validated typed domain records, canonical payload binding, and the closed reason-code registry for the synthetic replay runtime. Everything the stores persist and every boundary the runtime checks is expressed through these records.

## Requirements

### Requirement: Common record header

Every persisted domain record carries schema_version 2, an opaque bounded id, a positive integer revision, and a UTC RFC3339 created_at, plus exactly the lifecycle fields its type lists.

#### Scenario: Record missing a header field

- **WHEN** a record is constructed without a required header field, or with revision 0 or a negative value
- **THEN** validation fails with INVALID_SCHEMA and the record is not persisted

#### Scenario: Oversized or non-opaque identifier

- **WHEN** a record id exceeds its bound or contains raw secret material instead of an opaque string
- **THEN** validation fails with INVALID_SCHEMA

### Requirement: Strict record validation

Records reject unknown fields, unknown enum values, unbounded lists, and dangling references at construction time; references to revisioned content are RecordRef pairs with positive revisions, and version vectors are sorted with at most one revision per id.

#### Scenario: Unknown field on inbound record data

- **WHEN** record data arrives carrying a field not in the record's defined set
- **THEN** validation fails with INVALID_SCHEMA rather than ignoring the field

#### Scenario: Invalid reference

- **WHEN** a record references an id with revision 0, or a version vector repeats an id
- **THEN** validation fails with INVALID_REFERENCE

### Requirement: Canonical payload binding

Payload bytes are bound before approval: validated UTF-8 JSON with sorted keys, no insignificant whitespace, finite numeric values only, no unpaired surrogates; semantically unordered id sets are sorted while action/content order is preserved; text is never normalized after hashing; SHA-256 digests are computed over the encoded bytes.

#### Scenario: Equivalent unordered sets produce identical digests

- **WHEN** the same logical payload is supplied with its unordered id sets in two different orders
- **THEN** both produce identical canonical bytes and identical payload digests

#### Scenario: Non-finite numeric value

- **WHEN** payload data contains NaN or Infinity
- **THEN** canonical binding fails with INVALID_SCHEMA

#### Scenario: Ordered content is not reordered

- **WHEN** a payload carries an ordered content list
- **THEN** canonical binding preserves the list order byte-for-byte

### Requirement: Runtime-only field discipline

Authority, lease, epoch, producer-coverage, and verified-actor fields are populated only by runtime constructors; model-facing decision records reject them.

#### Scenario: Fabricated authorization inside model output

- **WHEN** a model-facing decision payload contains CommitAuthorization, lease, epoch, or coverage fields
- **THEN** the payload is invalid by construction and rejected with INVALID_SCHEMA

### Requirement: Closed reason-code registry

Error and reason codes come only from the fixed registry; unknown codes fail validation, and the registry is extended only through an explicit schema change with tests.

#### Scenario: Unknown reason code

- **WHEN** any record or result carries a code outside the registry
- **THEN** validation fails with INVALID_SCHEMA

### Requirement: Typed event kinds

Inbound event kinds are exactly the closed set the handoff lists; unlisted kinds are future typed extensions and are never mapped onto existing kinds or treated as owner instructions.

#### Scenario: Unlisted event kind

- **WHEN** record data arrives with an event kind outside the closed set
- **THEN** it fails record validation as an unknown enum value (INVALID_SCHEMA) rather than being remapped; operation-level unsupported kinds (e.g. an effect kind the slice does not enable) are refused with UNSUPPORTED at the commit boundary
