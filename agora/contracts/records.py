"""The §4 record set: common header base and every persisted record type."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import ClassVar

from agora.contracts.enums import (
    AccessStatus,
    ApprovalRule,
    AssertionStatus,
    AssessmentResult,
    AuthorityDomain,
    AuthorizationStatus,
    CommitmentStrength,
    Completeness,
    DecisionKind,
    DeliveryState,
    DerivationState,
    Disposition,
    EffectKind,
    EligibilityStatus,
    EnvelopePurpose,
    EnvironmentKind,
    EventKind,
    ExposureClass,
    FragmentKind,
    HistoryVisibility,
    IntendedUse,
    IntentState,
    LeaseStatus,
    MemoryStage,
    MemoryStatus,
    OutcomeKind,
    PerceptionStatus,
    PrincipalKind,
    ProducerKind,
    ReasonCode,
    RecordStatus,
    RecoveryStatus,
    RequestedPrecision,
    ReservationState,
    ResourceOperation,
    ReviewCoverage,
    ReviewRoute,
    ScopeKind,
    SinkKind,
    SinkStatus,
    SnapshotStatus,
    ViewKind,
    Visibility,
    WorkState,
)
from agora.contracts.errors import ValidationError
from agora.contracts.values import (
    MAX_ID_LEN,
    MAX_LIST_LEN,
    RECORD_SCHEMA_VERSION,
    Pair,
    Provenance,
    RecordRef,
    RuntimeOrigin,
    _is_int,
    check_bool,
    check_enum,
    check_id,
    check_non_negative_int,
    check_positive_int,
    check_sha256,
    check_str,
    check_utc,
    check_version_vector,
    enum_tuple,
    id_tuple,
    pair_tuple,
    ref_tuple,
    sorted_pair_tuple,
)

# ---------------------------------------------------------------------------
# Record base (handoff §3 common header)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Record:
    """Common header for every persisted domain record.

    Subclasses add exactly the fields their §4 table lists and override
    ``_validate`` for type-specific rules; the header is validated here.
    """

    TYPE_NAME: ClassVar[str] = "Record"

    schema_version: int = RECORD_SCHEMA_VERSION
    id: str
    revision: int
    created_at: str

    def __post_init__(self) -> None:
        if not _is_int(self.schema_version) or self.schema_version != RECORD_SCHEMA_VERSION:
            raise ValidationError(
                f"schema_version must be {RECORD_SCHEMA_VERSION}", path="schema_version"
            )
        check_id(self.id, path="id")
        check_positive_int(self.revision, path="revision")
        check_utc(self.created_at, path="created_at")
        self._normalize()
        self._validate()

    def _normalize(self) -> None:
        """Coerce valid string enum inputs to enum members so records hold
        closed-vocabulary values regardless of construction style. Invalid
        strings fall through to ``_validate`` for the proper error."""
        from agora.contracts.codec import (  # lazy: avoids import cycle
            _CODEC_ENUM,
            _CODEC_SEQ,
            _field_plan,
        )

        for name, (kind, param) in _field_plan(type(self)).items():
            value = getattr(self, name)
            if kind == _CODEC_ENUM and isinstance(value, str):
                with contextlib.suppress(ValueError):
                    object.__setattr__(self, name, param(value))
            elif kind == _CODEC_SEQ:
                item_kind, item_param = param
                if item_kind == _CODEC_ENUM:
                    converted = []
                    for item in value:
                        if isinstance(item, str):
                            try:
                                converted.append(item_param(item))
                            except ValueError:
                                converted.append(item)
                        else:
                            converted.append(item)
                    object.__setattr__(self, name, tuple(converted))

    def _validate(self) -> None:
        """Hook for type-specific validation; overridden by subclasses."""


# ---------------------------------------------------------------------------
# Records: identity, authority, event and context (handoff §4.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Principal(Record):
    TYPE_NAME: ClassVar[str] = "Principal"

    kind: PrincipalKind
    verified_bindings: tuple[RecordRef, ...]
    claimed_aliases: tuple[str, ...]

    def _validate(self) -> None:
        check_enum(self.kind, PrincipalKind, "kind")
        ref_tuple(self.verified_bindings, "verified_bindings")
        if len(self.claimed_aliases) > MAX_LIST_LEN:
            raise ValidationError(f"list exceeds bound of {MAX_LIST_LEN}", path="claimed_aliases")
        for alias in self.claimed_aliases:
            check_str(alias, "claimed_aliases")


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentIdentity(Record):
    TYPE_NAME: ClassVar[str] = "AgentIdentity"

    principal_ref: RecordRef
    preference_refs: tuple[RecordRef, ...]
    autobiography_refs: tuple[RecordRef, ...]
    commitment_refs: tuple[RecordRef, ...]
    active_interest_refs: tuple[RecordRef, ...]

    def _validate(self) -> None:
        if not isinstance(self.principal_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="principal_ref")
        ref_tuple(self.preference_refs, "preference_refs")
        ref_tuple(self.autobiography_refs, "autobiography_refs")
        ref_tuple(self.commitment_refs, "commitment_refs")
        ref_tuple(self.active_interest_refs, "active_interest_refs")


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorityBinding(Record):
    TYPE_NAME: ClassVar[str] = "AuthorityBinding"

    issuer_ref: RecordRef
    domain: AuthorityDomain
    resource_id: str
    evidence_ref: RecordRef
    delegated_from: RecordRef | None
    valid_until: str | None
    revoked_at: str | None

    def _validate(self) -> None:
        check_enum(self.domain, AuthorityDomain, "domain")
        check_id(self.resource_id, path="resource_id")
        if self.valid_until is not None:
            check_utc(self.valid_until, path="valid_until")
        if self.revoked_at is not None:
            check_utc(self.revoked_at, path="revoked_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourcePolicy(Record):
    TYPE_NAME: ClassVar[str] = "ResourcePolicy"

    resource_id: str
    data_class: str
    approval_rule: ApprovalRule
    eligible_issuer_refs: tuple[RecordRef, ...]
    commitment_refs: tuple[RecordRef, ...]
    allowed_transform_ids: tuple[str, ...]
    policy_revision: int
    status: RecordStatus

    def _validate(self) -> None:
        check_id(self.resource_id, path="resource_id")
        check_str(self.data_class, "data_class", max_chars=MAX_ID_LEN)
        check_enum(self.approval_rule, ApprovalRule, "approval_rule")
        ref_tuple(self.eligible_issuer_refs, "eligible_issuer_refs")
        ref_tuple(self.commitment_refs, "commitment_refs")
        id_tuple(self.allowed_transform_ids, "allowed_transform_ids")
        check_positive_int(self.policy_revision, path="policy_revision")
        check_enum(self.status, RecordStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class UseAuthorization(Record):
    """The tabulated ``revision`` is the common header revision (design D5)."""

    TYPE_NAME: ClassVar[str] = "UseAuthorization"

    resource_id: str
    issuer_refs: tuple[RecordRef, ...]
    binding_refs: tuple[RecordRef, ...]
    operations: tuple[ResourceOperation, ...]
    permitted_purpose_classes: tuple[str, ...]
    audience_class_bounds: tuple[str, ...]
    maximum_precision: RequestedPrecision
    allowed_transform_ids: tuple[str, ...]
    valid_until: str | None
    status: RecordStatus

    def _validate(self) -> None:
        check_id(self.resource_id, path="resource_id")
        ref_tuple(self.issuer_refs, "issuer_refs")
        ref_tuple(self.binding_refs, "binding_refs")
        enum_tuple(self.operations, ResourceOperation, "operations")
        id_tuple(self.permitted_purpose_classes, "permitted_purpose_classes")
        id_tuple(self.audience_class_bounds, "audience_class_bounds")
        check_enum(self.maximum_precision, RequestedPrecision, "maximum_precision")
        id_tuple(self.allowed_transform_ids, "allowed_transform_ids")
        if self.valid_until is not None:
            check_utc(self.valid_until, path="valid_until")
        check_enum(self.status, RecordStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityEnvelope(Record):
    TYPE_NAME: ClassVar[str] = "CapabilityEnvelope"

    actor_ref: RecordRef
    account_id: str
    allowed_destination_ids: tuple[str, ...]
    allowed_effect_kinds: tuple[EffectKind, ...]
    provisioning_binding_refs: tuple[RecordRef, ...]
    rate_budget_limits: tuple[Pair, ...]
    valid_until: str | None
    status: RecordStatus

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rate_budget_limits",
            sorted_pair_tuple(self.rate_budget_limits, "rate_budget_limits"),
        )
        super().__post_init__()

    def _validate(self) -> None:
        if not isinstance(self.actor_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="actor_ref")
        check_id(self.account_id, path="account_id")
        id_tuple(self.allowed_destination_ids, "allowed_destination_ids")
        enum_tuple(self.allowed_effect_kinds, EffectKind, "allowed_effect_kinds")
        ref_tuple(self.provisioning_binding_refs, "provisioning_binding_refs")
        if self.valid_until is not None:
            check_utc(self.valid_until, path="valid_until")
        check_enum(self.status, RecordStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class Commitment(Record):
    TYPE_NAME: ClassVar[str] = "Commitment"

    subject_refs: tuple[RecordRef, ...]
    custodian_refs: tuple[RecordRef, ...]
    resource_id: str | None
    source_scope: str | None
    strength: CommitmentStrength
    internal_use_allowed: bool
    observable_influence_allowed: bool
    expression_allowed: bool
    audience_predicate_id: str
    policy_evidence_refs: tuple[RecordRef, ...]

    def _validate(self) -> None:
        ref_tuple(self.subject_refs, "subject_refs")
        ref_tuple(self.custodian_refs, "custodian_refs")
        if (self.resource_id is None) == (self.source_scope is None):
            raise ValidationError(
                "exactly one of resource_id or source_scope must be set", path="resource_id"
            )
        if self.resource_id is not None:
            check_id(self.resource_id, path="resource_id")
        if self.source_scope is not None:
            check_id(self.source_scope, path="source_scope")
        check_enum(self.strength, CommitmentStrength, "strength")
        check_bool(self.internal_use_allowed, "internal_use_allowed")
        check_bool(self.observable_influence_allowed, "observable_influence_allowed")
        check_bool(self.expression_allowed, "expression_allowed")
        check_id(self.audience_predicate_id, path="audience_predicate_id")
        ref_tuple(self.policy_evidence_refs, "policy_evidence_refs")


@dataclass(frozen=True, slots=True, kw_only=True)
class InboundEvent(Record):
    TYPE_NAME: ClassVar[str] = "InboundEvent"

    source_event_id: str
    journal_seq: int
    kind: EventKind
    actor_ref: RecordRef
    occurred_at: str
    ingested_at: str
    source_audience_ref: RecordRef
    fragment_refs: tuple[RecordRef, ...]
    reply_target_refs: tuple[RecordRef, ...]
    target_refs: tuple[RecordRef, ...]
    observed_trigger_refs: tuple[RecordRef, ...]
    recovery: RecoveryStatus

    def _validate(self) -> None:
        check_id(self.source_event_id, path="source_event_id")
        check_non_negative_int(self.journal_seq, path="journal_seq")
        check_enum(self.kind, EventKind, "kind")
        check_utc(self.occurred_at, path="occurred_at")
        check_utc(self.ingested_at, path="ingested_at")
        ref_tuple(self.fragment_refs, "fragment_refs")
        ref_tuple(self.reply_target_refs, "reply_target_refs")
        ref_tuple(self.target_refs, "target_refs")
        ref_tuple(self.observed_trigger_refs, "observed_trigger_refs")
        check_enum(self.recovery, RecoveryStatus, "recovery")


@dataclass(frozen=True, slots=True, kw_only=True)
class AudienceSnapshot(Record):
    TYPE_NAME: ClassVar[str] = "AudienceSnapshot"

    destination_id: str
    environment_kind: EnvironmentKind
    visibility: Visibility
    known_principal_refs: tuple[RecordRef, ...]
    unknown_readers: bool
    history_visibility: HistoryVisibility
    completeness: Completeness
    disclosure_context_revision: int
    valid_until: str | None

    def _validate(self) -> None:
        check_id(self.destination_id, path="destination_id")
        check_enum(self.environment_kind, EnvironmentKind, "environment_kind")
        check_enum(self.visibility, Visibility, "visibility")
        ref_tuple(self.known_principal_refs, "known_principal_refs")
        check_bool(self.unknown_readers, "unknown_readers")
        check_enum(self.history_visibility, HistoryVisibility, "history_visibility")
        check_enum(self.completeness, Completeness, "completeness")
        check_positive_int(self.disclosure_context_revision, path="disclosure_context_revision")
        if self.valid_until is not None:
            check_utc(self.valid_until, path="valid_until")


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationEpisode(Record):
    TYPE_NAME: ClassVar[str] = "ConversationEpisode"

    container_id: str
    member_event_refs: tuple[RecordRef, ...]
    reply_edges: tuple[Pair, ...]
    topic_refs: tuple[RecordRef, ...]
    summary_ref: RecordRef | None
    episode_revision: int
    pending_intent_refs: tuple[RecordRef, ...]

    def _validate(self) -> None:
        check_id(self.container_id, path="container_id")
        ref_tuple(self.member_event_refs, "member_event_refs")
        pair_tuple(self.reply_edges, "reply_edges")
        ref_tuple(self.topic_refs, "topic_refs")
        check_positive_int(self.episode_revision, path="episode_revision")
        ref_tuple(self.pending_intent_refs, "pending_intent_refs")


@dataclass(frozen=True, slots=True, kw_only=True)
class ContentFragment(Record):
    TYPE_NAME: ClassVar[str] = "ContentFragment"

    kind: FragmentKind
    payload_ref: str
    provenance: Provenance
    commitment_refs: tuple[RecordRef, ...]
    eligibility_status: EligibilityStatus

    def _validate(self) -> None:
        check_enum(self.kind, FragmentKind, "kind")
        check_id(self.payload_ref, path="payload_ref")
        if not isinstance(self.provenance, Provenance):
            raise ValidationError("expected a Provenance value", path="provenance")
        ref_tuple(self.commitment_refs, "commitment_refs")
        check_enum(self.eligibility_status, EligibilityStatus, "eligibility_status")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProducerCoverage(Record):
    TYPE_NAME: ClassVar[str] = "ProducerCoverage"

    producer_kind: ProducerKind
    input_fragment_refs: tuple[RecordRef, ...]
    input_view_refs: tuple[RecordRef, ...]
    input_intent_refs: tuple[RecordRef, ...]
    input_policy_refs: tuple[RecordRef, ...]
    sink_ref: RecordRef | None
    call_id: str
    runtime_epoch: int

    def _validate(self) -> None:
        check_enum(self.producer_kind, ProducerKind, "producer_kind")
        ref_tuple(self.input_fragment_refs, "input_fragment_refs")
        ref_tuple(self.input_view_refs, "input_view_refs")
        ref_tuple(self.input_intent_refs, "input_intent_refs")
        ref_tuple(self.input_policy_refs, "input_policy_refs")
        check_str(self.call_id, "call_id", max_chars=MAX_ID_LEN)
        check_non_negative_int(self.runtime_epoch, path="runtime_epoch")


# ---------------------------------------------------------------------------
# Records: intent, work and memory (handoff §4.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Intent(Record):
    TYPE_NAME: ClassVar[str] = "Intent"

    creator_ref: RecordRef
    goal_fragment_ref: RecordRef
    purpose_class: str
    observed_trigger_refs: tuple[RecordRef, ...]
    endorsement_revision: int
    producer_coverage_ref: RecordRef | None
    runtime_task_root_ids: tuple[str, ...]
    parent_work_ref: RecordRef | None
    allowed_destination_refs: tuple[str, ...]
    due_at: str | None
    expires_at: str | None
    state: IntentState

    def _validate(self) -> None:
        if not isinstance(self.creator_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="creator_ref")
        if not isinstance(self.goal_fragment_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="goal_fragment_ref")
        check_id(self.purpose_class, path="purpose_class")
        ref_tuple(self.observed_trigger_refs, "observed_trigger_refs")
        check_positive_int(self.endorsement_revision, path="endorsement_revision")
        id_tuple(self.runtime_task_root_ids, "runtime_task_root_ids")
        id_tuple(self.allowed_destination_refs, "allowed_destination_refs")
        if self.due_at is not None:
            check_utc(self.due_at, path="due_at")
        if self.expires_at is not None:
            check_utc(self.expires_at, path="expires_at")
        check_enum(self.state, IntentState, "state")


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkItem(Record):
    TYPE_NAME: ClassVar[str] = "WorkItem"

    intent_ref: RecordRef
    trigger_key: str
    episode_ref: RecordRef | None
    state: WorkState
    lease_owner: str | None
    lease_until_ms: int | None
    runtime_epoch: int
    main_steps_used: int
    submissions_used: int
    terminal_outcome_ref: RecordRef | None

    def _validate(self) -> None:
        if not isinstance(self.intent_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="intent_ref")
        check_str(self.trigger_key, "trigger_key", max_chars=MAX_ID_LEN)
        check_enum(self.state, WorkState, "state")
        if self.lease_owner is not None:
            check_id(self.lease_owner, path="lease_owner")
        if self.lease_until_ms is not None:
            check_non_negative_int(self.lease_until_ms, path="lease_until_ms")
        check_non_negative_int(self.runtime_epoch, path="runtime_epoch")
        check_non_negative_int(self.main_steps_used, path="main_steps_used")
        check_non_negative_int(self.submissions_used, path="submissions_used")


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryRecord(Record):
    TYPE_NAME: ClassVar[str] = "MemoryRecord"

    assertion_fragment_ref: RecordRef
    stage: MemoryStage
    scope_kind: ScopeKind
    scope_ref: str
    custodian_refs: tuple[RecordRef, ...]
    subject_refs: tuple[RecordRef, ...]
    commitment_refs: tuple[RecordRef, ...]
    assertion_status: AssertionStatus
    supersedes_ref: RecordRef | None
    valid_until: str | None
    status: MemoryStatus

    def _validate(self) -> None:
        if not isinstance(self.assertion_fragment_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="assertion_fragment_ref")
        check_enum(self.stage, MemoryStage, "stage")
        check_enum(self.scope_kind, ScopeKind, "scope_kind")
        check_id(self.scope_ref, path="scope_ref")
        ref_tuple(self.custodian_refs, "custodian_refs")
        ref_tuple(self.subject_refs, "subject_refs")
        ref_tuple(self.commitment_refs, "commitment_refs")
        check_enum(self.assertion_status, AssertionStatus, "assertion_status")
        if self.valid_until is not None:
            check_utc(self.valid_until, path="valid_until")
        check_enum(self.status, MemoryStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class Derivation(Record):
    TYPE_NAME: ClassVar[str] = "Derivation"

    output_fragment_ref: RecordRef
    producer_coverage_ref: RecordRef
    claimed_parent_refs: tuple[RecordRef, ...]
    transform_id: str | None
    authorization_refs: tuple[RecordRef, ...]
    inherited_commitment_refs: tuple[RecordRef, ...]
    input_version_vector: tuple[RecordRef, ...]
    state: DerivationState

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_version_vector",
            check_version_vector(self.input_version_vector, "input_version_vector"),
        )
        super().__post_init__()

    def _validate(self) -> None:
        if not isinstance(self.output_fragment_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="output_fragment_ref")
        if not isinstance(self.producer_coverage_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="producer_coverage_ref")
        ref_tuple(self.claimed_parent_refs, "claimed_parent_refs")
        if self.transform_id is not None:
            check_id(self.transform_id, path="transform_id")
        ref_tuple(self.authorization_refs, "authorization_refs")
        ref_tuple(self.inherited_commitment_refs, "inherited_commitment_refs")
        check_enum(self.state, DerivationState, "state")


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryAccessRequest(Record):
    """``necessity_text`` XOR ``necessity_payload_ref`` (design D5): the
    model-facing form carries text; persistence swaps it to a payload ref."""

    TYPE_NAME: ClassVar[str] = "MemoryAccessRequest"

    request_id: str
    intent_ref: RecordRef
    resource_id: str | None
    data_class: str | None
    requested_precision: RequestedPrecision
    requested_fields: tuple[str, ...]
    transform_id: str | None
    audience_ref: RecordRef
    intended_use: IntendedUse
    necessity_text: str | None
    necessity_payload_ref: str | None
    trigger_refs: tuple[RecordRef, ...]
    runtime_origin: RuntimeOrigin | None

    def _validate(self) -> None:
        check_id(self.request_id, path="request_id")
        if not isinstance(self.intent_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="intent_ref")
        if (self.resource_id is None) == (self.data_class is None):
            raise ValidationError(
                "exactly one of resource_id or data_class must be set", path="resource_id"
            )
        if self.resource_id is not None:
            check_id(self.resource_id, path="resource_id")
        if self.data_class is not None:
            check_str(self.data_class, "data_class", max_chars=MAX_ID_LEN)
        check_enum(self.requested_precision, RequestedPrecision, "requested_precision")
        id_tuple(self.requested_fields, "requested_fields")
        if self.transform_id is not None:
            check_id(self.transform_id, path="transform_id")
        if not isinstance(self.audience_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="audience_ref")
        check_enum(self.intended_use, IntendedUse, "intended_use")
        if (self.necessity_text is None) == (self.necessity_payload_ref is None):
            raise ValidationError(
                "exactly one of necessity_text or necessity_payload_ref must be set",
                path="necessity_text",
            )
        if self.necessity_text is not None:
            check_str(self.necessity_text, "necessity_text")
        if self.necessity_payload_ref is not None:
            check_id(self.necessity_payload_ref, path="necessity_payload_ref")
        ref_tuple(self.trigger_refs, "trigger_refs")
        if self.runtime_origin is not None and not isinstance(self.runtime_origin, RuntimeOrigin):
            raise ValidationError("expected a RuntimeOrigin value", path="runtime_origin")


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryAccessResult(Record):
    """Widened per addendum D07 (which outranks the handoff table): the result
    itself exposes independent use permissions and basis references —
    references only, never duplicated payload bytes (design D4.2)."""

    TYPE_NAME: ClassVar[str] = "MemoryAccessResult"

    request_id: str
    access_status: AccessStatus
    view_kind: ViewKind
    view_ref: RecordRef | None
    reason_codes: tuple[ReasonCode, ...]
    safe_feedback_template_id: str | None
    permitted_uses: tuple[IntendedUse, ...]
    producer_coverage_ref: RecordRef | None
    transform_certificate_ref: RecordRef | None
    authorization_refs: tuple[RecordRef, ...]
    inherited_commitment_refs: tuple[RecordRef, ...]

    def _validate(self) -> None:
        check_id(self.request_id, path="request_id")
        check_enum(self.access_status, AccessStatus, "access_status")
        check_enum(self.view_kind, ViewKind, "view_kind")
        enum_tuple(self.reason_codes, ReasonCode, "reason_codes")
        if self.safe_feedback_template_id is not None:
            check_id(self.safe_feedback_template_id, path="safe_feedback_template_id")
        enum_tuple(self.permitted_uses, IntendedUse, "permitted_uses")
        ref_tuple(self.authorization_refs, "authorization_refs")
        ref_tuple(self.inherited_commitment_refs, "inherited_commitment_refs")
        if self.access_status == AccessStatus.GRANTED:
            if self.view_kind == ViewKind.NONE or self.view_ref is None:
                raise ValidationError(
                    "GRANTED requires FULL/PARTIAL/ABSTRACTED with an active view ref",
                    path="view_kind",
                )
        elif self.view_kind != ViewKind.NONE or self.view_ref is not None:
            raise ValidationError(
                "non-GRANTED results carry view_kind NONE and no view", path="view_kind"
            )
        elif self.permitted_uses or self.transform_certificate_ref is not None:
            raise ValidationError(
                "non-GRANTED results carry no permitted uses and no transform basis",
                path="permitted_uses",
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryView(Record):
    TYPE_NAME: ClassVar[str] = "MemoryView"

    resource_id: str
    returned_fragment_refs: tuple[RecordRef, ...]
    authorization_refs: tuple[RecordRef, ...]
    transform_certificate_ref: RecordRef | None
    producer_coverage_ref: RecordRef
    precision: RequestedPrecision
    inherited_commitment_refs: tuple[RecordRef, ...]
    permitted_uses: tuple[IntendedUse, ...]
    audience_bounds: tuple[str, ...]
    valid_until: str | None
    status: RecordStatus

    def _validate(self) -> None:
        check_id(self.resource_id, path="resource_id")
        ref_tuple(self.returned_fragment_refs, "returned_fragment_refs")
        ref_tuple(self.authorization_refs, "authorization_refs")
        if not isinstance(self.producer_coverage_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="producer_coverage_ref")
        check_enum(self.precision, RequestedPrecision, "precision")
        ref_tuple(self.inherited_commitment_refs, "inherited_commitment_refs")
        enum_tuple(self.permitted_uses, IntendedUse, "permitted_uses")
        id_tuple(self.audience_bounds, "audience_bounds")
        if self.valid_until is not None:
            check_utc(self.valid_until, path="valid_until")
        check_enum(self.status, RecordStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class TransformCertificate(Record):
    TYPE_NAME: ClassVar[str] = "TransformCertificate"

    registry_transform_id: str
    parameter_values: tuple[Pair, ...]
    source_coverage_ref: RecordRef
    authorizing_binding_refs: tuple[RecordRef, ...]
    authorizing_use_refs: tuple[RecordRef, ...]
    discharged_commitment_refs: tuple[RecordRef, ...]
    retained_commitment_refs: tuple[RecordRef, ...]
    output_view_ref: RecordRef

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parameter_values",
            sorted_pair_tuple(self.parameter_values, "parameter_values"),
        )
        super().__post_init__()

    def _validate(self) -> None:
        check_id(self.registry_transform_id, path="registry_transform_id")
        if not isinstance(self.source_coverage_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="source_coverage_ref")
        ref_tuple(self.authorizing_binding_refs, "authorizing_binding_refs")
        ref_tuple(self.authorizing_use_refs, "authorizing_use_refs")
        ref_tuple(self.discharged_commitment_refs, "discharged_commitment_refs")
        ref_tuple(self.retained_commitment_refs, "retained_commitment_refs")
        if not isinstance(self.output_view_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="output_view_ref")


# ---------------------------------------------------------------------------
# Records: model/context, decision and effect (handoff §4.3, §4.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ComputeSink(Record):
    TYPE_NAME: ClassVar[str] = "ComputeSink"

    kind: SinkKind
    configured_endpoint: str | None
    permitted_data_classes: tuple[str, ...]
    capability_ref: RecordRef | None
    status: SinkStatus

    def _validate(self) -> None:
        check_enum(self.kind, SinkKind, "kind")
        if self.configured_endpoint is not None:
            check_str(self.configured_endpoint, "configured_endpoint")
        id_tuple(self.permitted_data_classes, "permitted_data_classes")
        check_enum(self.status, SinkStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextSnapshot(Record):
    TYPE_NAME: ClassVar[str] = "ContextSnapshot"

    identity_ref: RecordRef
    intent_ref: RecordRef
    audience_ref: RecordRef
    episode_ref: RecordRef | None
    selected_fragment_refs: tuple[RecordRef, ...]
    active_view_refs: tuple[RecordRef, ...]
    producer_coverage_ref: RecordRef
    exposure_class: ExposureClass
    inherited_commitment_refs: tuple[RecordRef, ...]
    version_vector: tuple[RecordRef, ...]
    sink_ref: RecordRef
    effect_capable: bool
    serializer_version: str
    status: SnapshotStatus

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "version_vector", check_version_vector(self.version_vector, "version_vector")
        )
        super().__post_init__()

    def _validate(self) -> None:
        if not isinstance(self.identity_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="identity_ref")
        if not isinstance(self.intent_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="intent_ref")
        if not isinstance(self.audience_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="audience_ref")
        ref_tuple(self.selected_fragment_refs, "selected_fragment_refs")
        ref_tuple(self.active_view_refs, "active_view_refs")
        if not isinstance(self.producer_coverage_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="producer_coverage_ref")
        check_enum(self.exposure_class, ExposureClass, "exposure_class")
        ref_tuple(self.inherited_commitment_refs, "inherited_commitment_refs")
        if not isinstance(self.sink_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="sink_ref")
        check_bool(self.effect_capable, "effect_capable")
        check_str(self.serializer_version, "serializer_version", max_chars=MAX_ID_LEN)
        check_enum(self.status, SnapshotStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelCallEnvelope(Record):
    TYPE_NAME: ClassVar[str] = "ModelCallEnvelope"

    snapshot_ref: RecordRef
    purpose: EnvelopePurpose
    exact_messages_payload_ref: str
    tool_schema_digest: str
    supplied_fragment_refs: tuple[RecordRef, ...]
    supplied_view_refs: tuple[RecordRef, ...]
    sink_ref: RecordRef
    request_digest: str

    def _validate(self) -> None:
        if not isinstance(self.snapshot_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="snapshot_ref")
        check_enum(self.purpose, EnvelopePurpose, "purpose")
        check_id(self.exact_messages_payload_ref, path="exact_messages_payload_ref")
        check_sha256(self.tool_schema_digest, path="tool_schema_digest")
        ref_tuple(self.supplied_fragment_refs, "supplied_fragment_refs")
        ref_tuple(self.supplied_view_refs, "supplied_view_refs")
        if not isinstance(self.sink_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="sink_ref")
        check_sha256(self.request_digest, path="request_digest")


@dataclass(frozen=True, slots=True, kw_only=True)
class PerceptionSignals(Record):
    TYPE_NAME: ClassVar[str] = "PerceptionSignals"

    event_refs: tuple[RecordRef, ...]
    window_refs: tuple[RecordRef, ...]
    probabilities: tuple[Pair, ...]
    address_flag: bool
    reply_flag: bool
    status: PerceptionStatus
    adapter_version: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "probabilities", sorted_pair_tuple(self.probabilities, "probabilities")
        )
        super().__post_init__()

    def _validate(self) -> None:
        ref_tuple(self.event_refs, "event_refs")
        ref_tuple(self.window_refs, "window_refs")
        check_bool(self.address_flag, "address_flag")
        check_bool(self.reply_flag, "reply_flag")
        check_enum(self.status, PerceptionStatus, "status")
        check_str(self.adapter_version, "adapter_version", max_chars=MAX_ID_LEN)


@dataclass(frozen=True, slots=True, kw_only=True)
class Effect(Record):
    TYPE_NAME: ClassVar[str] = "Effect"

    logical_effect_id: str
    kind: EffectKind
    account_id: str
    destination_id: str
    target_ref: RecordRef | None
    rendered_payload_ref: str
    payload_digest: str
    capability_ref: RecordRef

    def _validate(self) -> None:
        check_id(self.logical_effect_id, path="logical_effect_id")
        check_enum(self.kind, EffectKind, "kind")
        check_id(self.account_id, path="account_id")
        check_id(self.destination_id, path="destination_id")
        if self.kind == EffectKind.ADD_REACTION and self.target_ref is None:
            raise ValidationError("ADD_REACTION requires a target ref", path="target_ref")
        check_id(self.rendered_payload_ref, path="rendered_payload_ref")
        check_sha256(self.payload_digest, path="payload_digest")
        if not isinstance(self.capability_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="capability_ref")


@dataclass(frozen=True, slots=True, kw_only=True)
class EffectPlan(Record):
    TYPE_NAME: ClassVar[str] = "EffectPlan"

    intent_ref: RecordRef
    snapshot_ref: RecordRef
    disposition: Disposition
    effects: tuple[Effect, ...]
    proposal_revision: int
    proposal_digest: str
    defer_until: str | None
    defer_expires_at: str | None
    declared_fact_refs: tuple[RecordRef, ...]
    observed_target_refs: tuple[RecordRef, ...]
    producer_coverage_ref: RecordRef

    def _validate(self) -> None:
        if not isinstance(self.intent_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="intent_ref")
        if not isinstance(self.snapshot_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="snapshot_ref")
        check_enum(self.disposition, Disposition, "disposition")
        if not isinstance(self.effects, tuple) or len(self.effects) > 1:
            raise ValidationError("the slice supports at most one effect per plan", path="effects")
        for effect in self.effects:
            if not isinstance(effect, Effect):
                raise ValidationError("expected an Effect value", path="effects")
        check_positive_int(self.proposal_revision, path="proposal_revision")
        check_sha256(self.proposal_digest, path="proposal_digest")
        if self.disposition == Disposition.ACT and len(self.effects) != 1:
            raise ValidationError("ACT carries exactly one effect", path="effects")
        if self.disposition in (Disposition.QUIET, Disposition.DEFER) and self.effects:
            raise ValidationError("QUIET and DEFER carry zero effects", path="effects")
        if self.disposition == Disposition.DEFER:
            if self.defer_until is None or self.defer_expires_at is None:
                raise ValidationError(
                    "DEFER supplies defer_until and defer_expires_at", path="defer_until"
                )
            check_utc(self.defer_until, path="defer_until")
            check_utc(self.defer_expires_at, path="defer_expires_at")
        ref_tuple(self.declared_fact_refs, "declared_fact_refs")
        ref_tuple(self.observed_target_refs, "observed_target_refs")
        if not isinstance(self.producer_coverage_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="producer_coverage_ref")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewRequirement(Record):
    TYPE_NAME: ClassVar[str] = "ReviewRequirement"

    route: ReviewRoute
    reason_codes: tuple[ReasonCode, ...]
    relevant_commitment_refs: tuple[RecordRef, ...]
    expected_coverage: tuple[ReviewCoverage, ...]
    budget: int
    sink_ref: RecordRef
    policy_version_vector: tuple[RecordRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_version_vector",
            check_version_vector(self.policy_version_vector, "policy_version_vector"),
        )
        super().__post_init__()

    def _validate(self) -> None:
        check_enum(self.route, ReviewRoute, "route")
        enum_tuple(self.reason_codes, ReasonCode, "reason_codes")
        ref_tuple(self.relevant_commitment_refs, "relevant_commitment_refs")
        enum_tuple(self.expected_coverage, ReviewCoverage, "expected_coverage")
        check_non_negative_int(self.budget, path="budget")
        if not isinstance(self.sink_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="sink_ref")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewAssessment(Record):
    """The record id embeds plan id + proposal revision (design D4.3), making
    re-acceptance idempotent and 'at most one assessment per submission'
    structural at the store layer."""

    TYPE_NAME: ClassVar[str] = "ReviewAssessment"

    result: AssessmentResult
    flags: tuple[str, ...]
    runtime_assigned_coverage: ReviewCoverage
    supplied_view_refs: tuple[RecordRef, ...]
    proposal_digest: str
    policy_version_vector: tuple[RecordRef, ...]
    snapshot_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_version_vector",
            check_version_vector(self.policy_version_vector, "policy_version_vector"),
        )
        super().__post_init__()

    def _validate(self) -> None:
        check_enum(self.result, AssessmentResult, "result")
        id_tuple(self.flags, "flags")
        check_enum(self.runtime_assigned_coverage, ReviewCoverage, "runtime_assigned_coverage")
        ref_tuple(self.supplied_view_refs, "supplied_view_refs")
        check_sha256(self.proposal_digest, path="proposal_digest")
        check_positive_int(self.snapshot_revision, path="snapshot_revision")


@dataclass(frozen=True, slots=True, kw_only=True)
class CommitDecision(Record):
    TYPE_NAME: ClassVar[str] = "CommitDecision"

    kind: DecisionKind
    reason_codes: tuple[ReasonCode, ...]
    authorization_ref: RecordRef | None
    assessment_ref: RecordRef | None

    def _validate(self) -> None:
        check_enum(self.kind, DecisionKind, "kind")
        enum_tuple(self.reason_codes, ReasonCode, "reason_codes")


@dataclass(frozen=True, slots=True, kw_only=True)
class CommitAuthorization(Record):
    TYPE_NAME: ClassVar[str] = "CommitAuthorization"

    logical_effect_id: str
    proposal_revision: int
    proposal_digest: str
    payload_digest: str
    identity_ref: RecordRef
    account_id: str
    audience_disclosure_revision: int
    target_dependency_refs: tuple[RecordRef, ...]
    authority_version_vector: tuple[RecordRef, ...]
    policy_version_vector: tuple[RecordRef, ...]
    runtime_epoch: int
    capability_ref: RecordRef
    status: AuthorizationStatus

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "authority_version_vector",
            check_version_vector(self.authority_version_vector, "authority_version_vector"),
        )
        object.__setattr__(
            self,
            "policy_version_vector",
            check_version_vector(self.policy_version_vector, "policy_version_vector"),
        )
        super().__post_init__()

    def _validate(self) -> None:
        check_id(self.logical_effect_id, path="logical_effect_id")
        check_positive_int(self.proposal_revision, path="proposal_revision")
        check_sha256(self.proposal_digest, path="proposal_digest")
        check_sha256(self.payload_digest, path="payload_digest")
        if not isinstance(self.identity_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="identity_ref")
        check_id(self.account_id, path="account_id")
        check_positive_int(self.audience_disclosure_revision, path="audience_disclosure_revision")
        ref_tuple(self.target_dependency_refs, "target_dependency_refs")
        check_non_negative_int(self.runtime_epoch, path="runtime_epoch")
        if not isinstance(self.capability_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="capability_ref")
        check_enum(self.status, AuthorizationStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class DispatchLease(Record):
    """Runtime-only record; never model-facing."""

    TYPE_NAME: ClassVar[str] = "DispatchLease"

    lease_id: str
    attempt_id: str
    authorization_ref: RecordRef
    logical_effect_id: str
    payload_digest: str
    runtime_epoch: int
    transport_generation: int
    reservation_ref: RecordRef | None
    issued_at_ms: int
    expires_at_ms: int
    status: LeaseStatus

    def _validate(self) -> None:
        check_id(self.lease_id, path="lease_id")
        check_id(self.attempt_id, path="attempt_id")
        if not isinstance(self.authorization_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="authorization_ref")
        check_id(self.logical_effect_id, path="logical_effect_id")
        check_sha256(self.payload_digest, path="payload_digest")
        check_non_negative_int(self.runtime_epoch, path="runtime_epoch")
        check_non_negative_int(self.transport_generation, path="transport_generation")
        check_non_negative_int(self.issued_at_ms, path="issued_at_ms")
        check_non_negative_int(self.expires_at_ms, path="expires_at_ms")
        if self.expires_at_ms < self.issued_at_ms:
            raise ValidationError("lease expires before it is issued", path="expires_at_ms")
        check_enum(self.status, LeaseStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class DisclosureReservation(Record):
    TYPE_NAME: ClassVar[str] = "DisclosureReservation"

    reservation_id: str
    constraint_group_id: str
    subject_resource_refs: tuple[RecordRef, ...]
    audience_class: str
    transform_query_key: str
    logical_effect_id: str
    state: ReservationState

    def _validate(self) -> None:
        check_id(self.reservation_id, path="reservation_id")
        check_id(self.constraint_group_id, path="constraint_group_id")
        ref_tuple(self.subject_resource_refs, "subject_resource_refs")
        check_id(self.audience_class, path="audience_class")
        check_id(self.transform_query_key, path="transform_query_key")
        check_id(self.logical_effect_id, path="logical_effect_id")
        check_enum(self.state, ReservationState, "state")


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliveryReceipt(Record):
    TYPE_NAME: ClassVar[str] = "DeliveryReceipt"

    effect_id: str
    proposal_revision: int
    attempt_id: str
    state: DeliveryState
    fake_remote_id: str | None
    generation: int
    error_code: ReasonCode | None
    observed_at: str

    def _validate(self) -> None:
        check_id(self.effect_id, path="effect_id")
        check_positive_int(self.proposal_revision, path="proposal_revision")
        check_id(self.attempt_id, path="attempt_id")
        check_enum(self.state, DeliveryState, "state")
        if self.fake_remote_id is not None:
            check_id(self.fake_remote_id, path="fake_remote_id")
        check_non_negative_int(self.generation, path="generation")
        if self.error_code is not None:
            check_enum(self.error_code, ReasonCode, "error_code")
        check_utc(self.observed_at, path="observed_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnOutcome(Record):
    TYPE_NAME: ClassVar[str] = "TurnOutcome"

    kind: OutcomeKind
    intent_ref: RecordRef | None
    work_ref: RecordRef | None
    reason_codes: tuple[ReasonCode, ...]
    effect_refs: tuple[RecordRef, ...]

    def _validate(self) -> None:
        check_enum(self.kind, OutcomeKind, "kind")
        enum_tuple(self.reason_codes, ReasonCode, "reason_codes")
        ref_tuple(self.effect_refs, "effect_refs")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewEnvelope(Record):
    """Closed field set per handoff §4.4 — no release-ledger field. The
    reviewer's cumulative-release visibility question is tracked separately
    (beads hermes-agora-2qm); extension path is a schema_version bump."""

    TYPE_NAME: ClassVar[str] = "ReviewEnvelope"

    plan_ref: RecordRef
    proposal_digest: str
    snapshot_ref: RecordRef
    audience_ref: RecordRef
    intent_ref: RecordRef
    relevant_policy_refs: tuple[RecordRef, ...]
    relevant_commitment_refs: tuple[RecordRef, ...]
    comparison_view_refs: tuple[RecordRef, ...]
    bounded_quote_fragment_refs: tuple[RecordRef, ...]
    expected_coverage_refs: tuple[RecordRef, ...]
    sink_ref: RecordRef
    exact_messages_payload_ref: str

    def _validate(self) -> None:
        if not isinstance(self.plan_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="plan_ref")
        check_sha256(self.proposal_digest, path="proposal_digest")
        if not isinstance(self.snapshot_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="snapshot_ref")
        if not isinstance(self.audience_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="audience_ref")
        if not isinstance(self.intent_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="intent_ref")
        ref_tuple(self.relevant_policy_refs, "relevant_policy_refs")
        ref_tuple(self.relevant_commitment_refs, "relevant_commitment_refs")
        ref_tuple(self.comparison_view_refs, "comparison_view_refs")
        ref_tuple(self.bounded_quote_fragment_refs, "bounded_quote_fragment_refs")
        ref_tuple(self.expected_coverage_refs, "expected_coverage_refs")
        if not isinstance(self.sink_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="sink_ref")
        check_id(self.exact_messages_payload_ref, path="exact_messages_payload_ref")


RECORD_TYPES: dict[str, type[Record]] = {
    cls.TYPE_NAME: cls
    for cls in (
        Principal,
        AgentIdentity,
        AuthorityBinding,
        ResourcePolicy,
        UseAuthorization,
        CapabilityEnvelope,
        Commitment,
        InboundEvent,
        AudienceSnapshot,
        ConversationEpisode,
        ContentFragment,
        ProducerCoverage,
        Intent,
        WorkItem,
        MemoryRecord,
        Derivation,
        MemoryAccessRequest,
        MemoryAccessResult,
        MemoryView,
        TransformCertificate,
        ComputeSink,
        ContextSnapshot,
        ModelCallEnvelope,
        PerceptionSignals,
        EffectPlan,
        Effect,
        ReviewRequirement,
        ReviewAssessment,
        CommitDecision,
        CommitAuthorization,
        DispatchLease,
        DisclosureReservation,
        DeliveryReceipt,
        TurnOutcome,
        ReviewEnvelope,
    )
}

ALL_RECORD_TYPES: tuple[type[Record], ...] = tuple(
    RECORD_TYPES[name] for name in sorted(RECORD_TYPES)
)
