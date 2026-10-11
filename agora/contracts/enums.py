"""Closed vocabularies and the safe reason-code registry (handoff §4)."""

from __future__ import annotations

from enum import StrEnum

# ---------------------------------------------------------------------------
# Safe reason-code registry (handoff §4.4) — closed; extension is an explicit
# schema change with tests.
# ---------------------------------------------------------------------------


class ReasonCode(StrEnum):
    UNSUPPORTED = "UNSUPPORTED"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    INVALID_REFERENCE = "INVALID_REFERENCE"
    AUTHORITY_MISSING = "AUTHORITY_MISSING"
    AUTHORITY_CONFLICT = "AUTHORITY_CONFLICT"
    POLICY_FORBIDDEN = "POLICY_FORBIDDEN"
    POLICY_CHANGED = "POLICY_CHANGED"
    VIEW_FORBIDDEN = "VIEW_FORBIDDEN"
    SINK_FORBIDDEN = "SINK_FORBIDDEN"
    CONTEXT_MISMATCH = "CONTEXT_MISMATCH"
    STALE_CONTEXT = "STALE_CONTEXT"
    AUDIENCE_CHANGED = "AUDIENCE_CHANGED"
    TARGET_CHANGED = "TARGET_CHANGED"
    CAPABILITY_INVALID = "CAPABILITY_INVALID"
    REVIEW_CONCERN = "REVIEW_CONCERN"
    REVIEW_FACTS_MISSING = "REVIEW_FACTS_MISSING"
    REVIEW_TIMEOUT = "REVIEW_TIMEOUT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    RELEASE_LIMIT = "RELEASE_LIMIT"
    STALE_EPOCH = "STALE_EPOCH"
    STALE_GENERATION = "STALE_GENERATION"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    LEASE_CONSUMED = "LEASE_CONSUMED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED_BEFORE_DISPATCH = "CANCELLED_BEFORE_DISPATCH"
    TOO_LATE = "TOO_LATE"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    CONFIRMED_NO_EFFECT = "CONFIRMED_NO_EFFECT"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
    NO_ACTION = "NO_ACTION"
    DEFERRED = "DEFERRED"
    COMMITTED = "COMMITTED"


REASON_CODES: frozenset[str] = frozenset(ReasonCode)


# ---------------------------------------------------------------------------
# Closed vocabularies (handoff §4). Unknown values fail validation; future
# kinds are new typed members, never reuse of an existing one.
# ---------------------------------------------------------------------------


class EventKind(StrEnum):
    MESSAGE_CREATED = "MESSAGE_CREATED"
    MESSAGE_UPDATED = "MESSAGE_UPDATED"
    MESSAGE_DELETED = "MESSAGE_DELETED"
    AUDIENCE_CHANGED = "AUDIENCE_CHANGED"
    POLICY_CHANGED = "POLICY_CHANGED"
    TIMER_DUE = "TIMER_DUE"
    AGENT_INTENT_CREATED = "AGENT_INTENT_CREATED"
    AGENT_STATE_ADOPTED = "AGENT_STATE_ADOPTED"
    CONNECTION_GAP = "CONNECTION_GAP"


class RecoveryStatus(StrEnum):
    COMPLETE = "COMPLETE"
    GAP = "GAP"
    UNKNOWN = "UNKNOWN"


class PrincipalKind(StrEnum):
    AGENT = "AGENT"
    HUMAN_OPERATOR = "HUMAN_OPERATOR"
    THIRD_PARTY = "THIRD_PARTY"


class AuthorityDomain(StrEnum):
    RESOURCE_USE = "RESOURCE_USE"
    DISCLOSURE = "DISCLOSURE"
    AGENT_STATE = "AGENT_STATE"
    DEPLOYMENT = "DEPLOYMENT"


class ApprovalRule(StrEnum):
    SINGLE_ISSUER = "SINGLE_ISSUER"
    ALL_LISTED = "ALL_LISTED"


class RecordStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class ResourceOperation(StrEnum):
    READ = "READ"
    TRANSFORM = "TRANSFORM"
    REVIEW = "REVIEW"
    STORE = "STORE"
    EXPRESS = "EXPRESS"
    OBSERVABLE_INFLUENCE = "OBSERVABLE_INFLUENCE"


class CommitmentStrength(StrEnum):
    ORDINARY_DISCRETION = "ORDINARY_DISCRETION"
    STRONG = "STRONG"


class EligibilityStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    TOMBSTONED = "TOMBSTONED"


class EnvironmentKind(StrEnum):
    ROOM = "ROOM"
    DM = "DM"
    INTERNAL = "INTERNAL"


class Visibility(StrEnum):
    PUBLIC = "PUBLIC"
    RESTRICTED = "RESTRICTED"
    PRIVATE_DM = "PRIVATE_DM"
    AGENT_PRIVATE = "AGENT_PRIVATE"


class HistoryVisibility(StrEnum):
    PUBLIC_HISTORY = "PUBLIC_HISTORY"
    RESTRICTED_HISTORY = "RESTRICTED_HISTORY"
    DM_PARTICIPANTS = "DM_PARTICIPANTS"
    UNKNOWN = "UNKNOWN"


class Completeness(StrEnum):
    OBSERVED = "OBSERVED"
    INCOMPLETE = "INCOMPLETE"


class FragmentKind(StrEnum):
    TEXT = "TEXT"
    STRUCTURED = "STRUCTURED"
    MODEL_OUTPUT = "MODEL_OUTPUT"
    SUMMARY = "SUMMARY"


class SourceKind(StrEnum):
    FIXTURE_CONTROL = "FIXTURE_CONTROL"
    SOCIAL_MESSAGE = "SOCIAL_MESSAGE"
    WEB_QUOTE = "WEB_QUOTE"
    TOOL_RESULT = "TOOL_RESULT"
    AGENT_OUTPUT = "AGENT_OUTPUT"
    MEMORY = "MEMORY"
    SUMMARY = "SUMMARY"
    INFERENCE = "INFERENCE"


class InstructionAuthority(StrEnum):
    CONTROL_PLANE = "CONTROL_PLANE"
    AGENT_STATE = "AGENT_STATE"
    NONE = "NONE"


class Integrity(StrEnum):
    RUNTIME_VERIFIED = "RUNTIME_VERIFIED"
    EXTERNAL_UNVERIFIED = "EXTERNAL_UNVERIFIED"
    DERIVED = "DERIVED"


class AssertionStatus(StrEnum):
    OBSERVED_STATEMENT = "OBSERVED_STATEMENT"
    ATTRIBUTED_ASSERTION = "ATTRIBUTED_ASSERTION"
    CONTESTED = "CONTESTED"
    INFERENCE = "INFERENCE"
    AGENT_ADOPTED_PREFERENCE = "AGENT_ADOPTED_PREFERENCE"


class ProducerKind(StrEnum):
    CONTEXT_ASSEMBLY = "CONTEXT_ASSEMBLY"
    MAIN = "MAIN"
    REVIEW = "REVIEW"
    SUMMARY = "SUMMARY"
    TRANSFORM = "TRANSFORM"


class IntentState(StrEnum):
    OPEN = "OPEN"
    DEFERRED = "DEFERRED"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


class WorkState(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    HOLD = "HOLD"
    COMPLETED = "COMPLETED"


class MemoryStage(StrEnum):
    EPHEMERAL = "EPHEMERAL"
    SOCIAL = "SOCIAL"
    CANDIDATE = "CANDIDATE"
    DURABLE = "DURABLE"


class ScopeKind(StrEnum):
    AGENT = "AGENT"
    RELATIONSHIP = "RELATIONSHIP"
    ROOM = "ROOM"
    PROJECT = "PROJECT"
    BROAD = "BROAD"


class MemoryStatus(StrEnum):
    ACTIVE = "ACTIVE"
    STALE = "STALE"
    TOMBSTONED = "TOMBSTONED"


class DerivationState(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"


class RequestedPrecision(StrEnum):
    EXACT = "EXACT"
    FIELDS = "FIELDS"
    ABSTRACT = "ABSTRACT"


class IntendedUse(StrEnum):
    INTERNAL_ONLY = "INTERNAL_ONLY"
    OBSERVABLE_INFLUENCE = "OBSERVABLE_INFLUENCE"
    EXPRESSION = "EXPRESSION"


class AccessStatus(StrEnum):
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    PENDING_AUTHORITY = "PENDING_AUTHORITY"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICT = "CONFLICT"


class ViewKind(StrEnum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    ABSTRACTED = "ABSTRACTED"
    NONE = "NONE"


class SinkKind(StrEnum):
    SCRIPTED_TEST = "SCRIPTED_TEST"
    LOCAL_MODEL = "LOCAL_MODEL"
    REMOTE_UNSUPPORTED = "REMOTE_UNSUPPORTED"


class SinkStatus(StrEnum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class EnvelopePurpose(StrEnum):
    MAIN_STEP = "MAIN_STEP"
    REVIEW = "REVIEW"
    SUMMARY = "SUMMARY"


class ExposureClass(StrEnum):
    ORDINARY = "ORDINARY"
    STRONG = "STRONG"


class SnapshotStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INVALID = "INVALID"


class PerceptionStatus(StrEnum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


class Disposition(StrEnum):
    ACT = "ACT"
    QUIET = "QUIET"
    DEFER = "DEFER"


class EffectKind(StrEnum):
    SEND_MESSAGE = "SEND_MESSAGE"
    ADD_REACTION = "ADD_REACTION"


class ReviewRoute(StrEnum):
    NONE = "NONE"
    ADVISORY = "ADVISORY"
    REQUIRED = "REQUIRED"
    HARD_DENY = "HARD_DENY"


class AssessmentResult(StrEnum):
    PASS = "PASS"
    CONCERNS = "CONCERNS"
    INSUFFICIENT_CONTEXT = "INSUFFICIENT_CONTEXT"


class ReviewCoverage(StrEnum):
    FACT_COMPARISON = "FACT_COMPARISON"
    DESCRIPTORS_ONLY = "DESCRIPTORS_ONLY"
    NO_FACT_ACCESS = "NO_FACT_ACCESS"


class DecisionKind(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    HOLD = "HOLD"
    DENIED = "DENIED"
    QUIET_COMPLETED = "QUIET_COMPLETED"
    DEFERRED = "DEFERRED"


class AuthorizationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"


class LeaseStatus(StrEnum):
    ISSUED = "ISSUED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class ReservationState(StrEnum):
    RESERVED = "RESERVED"
    COMMITTED = "COMMITTED"
    POSSIBLY_COMMITTED = "POSSIBLY_COMMITTED"
    CANCELLED_BEFORE_DISPATCH = "CANCELLED_BEFORE_DISPATCH"


class DeliveryState(StrEnum):
    SENT = "SENT"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"
    UNKNOWN = "UNKNOWN"


class OutcomeKind(StrEnum):
    NO_ACTION = "NO_ACTION"
    DEFERRED = "DEFERRED"
    COMMITTED = "COMMITTED"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    HELD = "HELD"
    FAILED_INTERNAL = "FAILED_INTERNAL"


class ParticipateMode(StrEnum):
    ENGAGE = "ENGAGE"
    REACT = "REACT"
    IGNORE = "IGNORE"
    EXPLORE = "EXPLORE"


class MainStepKind(StrEnum):
    PARTICIPATE = "PARTICIPATE"
    REQUEST_ACCESS = "REQUEST_ACCESS"
    PROPOSE_MEMORY = "PROPOSE_MEMORY"
    ADOPT_PREFERENCE = "ADOPT_PREFERENCE"
    CREATE_INTENT = "CREATE_INTENT"
    SUBMIT_PLAN = "SUBMIT_PLAN"
    FINISH = "FINISH"


class FinishChoice(StrEnum):
    NO_ACTION = "NO_ACTION"
    DEFER = "DEFER"


class DependencyKind(StrEnum):
    PRODUCER_INPUT = "PRODUCER_INPUT"
    DECLARED = "DECLARED"
    CONTROL = "CONTROL"


class PayloadStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PURGED = "PURGED"
