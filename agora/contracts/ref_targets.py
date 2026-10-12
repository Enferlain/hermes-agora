"""Typed-reference targets (handoff §7 core.records "typed refs" invariant).

The map assigns every RecordRef-bearing field the record type(s) its target
must have when the record is inserted. Fields whose targets are legitimately
polymorphic (version vectors, arbitrary dependencies) use the ANY_TARGET
sentinel: existence is still verified, the type is not.

The map is hand-authored from the §4 record semantics; the codec-derived
drift test in tests/test_ref_targets.py keeps it aligned with the actual
record fields, so a renamed or new ref field cannot silently skip
verification.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final

from agora.contracts.records import (
    AgentIdentity,
    AudienceSnapshot,
    AuthorityBinding,
    CapabilityEnvelope,
    CommitAuthorization,
    CommitDecision,
    Commitment,
    ComputeSink,
    ContentFragment,
    ContextSnapshot,
    ConversationEpisode,
    Derivation,
    DisclosureReservation,
    DispatchLease,
    Effect,
    EffectPlan,
    InboundEvent,
    Intent,
    MemoryAccessRequest,
    MemoryAccessResult,
    MemoryRecord,
    MemoryView,
    ModelCallEnvelope,
    PerceptionSignals,
    Principal,
    ProducerCoverage,
    Record,
    ResourcePolicy,
    ReviewAssessment,
    ReviewEnvelope,
    ReviewRequirement,
    TransformCertificate,
    TurnOutcome,
    UseAuthorization,
    WorkItem,
)
from agora.contracts.values import Provenance, RecordRef, RuntimeOrigin

#: Existence-only sentinel: any record type satisfies the check.
ANY_TARGET: Final = "*"

AllowedTargets = str | frozenset[str]

#: Commitments and the registry records that evidence them.
POLICY_LIKE: Final = frozenset({"Commitment", "ResourcePolicy", "UseAuthorization"})

REF_TARGETS: dict[type[Record], dict[str, AllowedTargets]] = {
    Principal: {"verified_bindings": "AuthorityBinding"},
    AgentIdentity: {
        "principal_ref": "Principal",
        "preference_refs": "ContentFragment",
        "autobiography_refs": "ContentFragment",
        "commitment_refs": "Commitment",
        "active_interest_refs": "ContentFragment",
    },
    AuthorityBinding: {
        "issuer_ref": "Principal",
        "evidence_ref": "ContentFragment",
        "delegated_from": "AuthorityBinding",
    },
    ResourcePolicy: {
        "eligible_issuer_refs": "Principal",
        "commitment_refs": "Commitment",
    },
    UseAuthorization: {
        "issuer_refs": "Principal",
        "binding_refs": "AuthorityBinding",
    },
    CapabilityEnvelope: {
        "actor_ref": "Principal",
        "provisioning_binding_refs": "AuthorityBinding",
    },
    Commitment: {
        "subject_refs": "Principal",
        "custodian_refs": "Principal",
        "policy_evidence_refs": frozenset({"ResourcePolicy", "UseAuthorization"}),
    },
    InboundEvent: {
        "actor_ref": "Principal",
        "source_audience_ref": "AudienceSnapshot",
        "fragment_refs": "ContentFragment",
        "reply_target_refs": "InboundEvent",
        "target_refs": ANY_TARGET,
        "observed_trigger_refs": "InboundEvent",
    },
    AudienceSnapshot: {"known_principal_refs": "Principal"},
    ConversationEpisode: {
        "member_event_refs": "InboundEvent",
        "topic_refs": "ContentFragment",
        "summary_ref": "ContentFragment",
        "pending_intent_refs": "Intent",
    },
    ContentFragment: {"commitment_refs": "Commitment"},
    ProducerCoverage: {
        "input_fragment_refs": "ContentFragment",
        "input_view_refs": "MemoryView",
        "input_intent_refs": "Intent",
        "input_policy_refs": POLICY_LIKE,
        "sink_ref": "ComputeSink",
    },
    Intent: {
        "creator_ref": "Principal",
        "goal_fragment_ref": "ContentFragment",
        "observed_trigger_refs": "InboundEvent",
        "producer_coverage_ref": "ProducerCoverage",
        "parent_work_ref": "WorkItem",
    },
    WorkItem: {
        "intent_ref": "Intent",
        "episode_ref": "ConversationEpisode",
        "terminal_outcome_ref": "TurnOutcome",
    },
    MemoryRecord: {
        "assertion_fragment_ref": "ContentFragment",
        "custodian_refs": "Principal",
        "subject_refs": "Principal",
        "commitment_refs": "Commitment",
        "supersedes_ref": "MemoryRecord",
    },
    Derivation: {
        "output_fragment_ref": "ContentFragment",
        "producer_coverage_ref": "ProducerCoverage",
        "claimed_parent_refs": "ContentFragment",
        "authorization_refs": "UseAuthorization",
        "inherited_commitment_refs": "Commitment",
        "input_version_vector": ANY_TARGET,
    },
    MemoryAccessRequest: {
        "intent_ref": "Intent",
        "audience_ref": "AudienceSnapshot",
        "trigger_refs": "InboundEvent",
    },
    MemoryAccessResult: {
        "view_ref": "MemoryView",
        "producer_coverage_ref": "ProducerCoverage",
        "transform_certificate_ref": "TransformCertificate",
        "authorization_refs": "UseAuthorization",
        "inherited_commitment_refs": "Commitment",
    },
    MemoryView: {
        "returned_fragment_refs": "ContentFragment",
        "authorization_refs": "UseAuthorization",
        "transform_certificate_ref": "TransformCertificate",
        "producer_coverage_ref": "ProducerCoverage",
        "inherited_commitment_refs": "Commitment",
    },
    TransformCertificate: {
        "source_coverage_ref": "ProducerCoverage",
        "authorizing_binding_refs": "AuthorityBinding",
        "authorizing_use_refs": "UseAuthorization",
        "discharged_commitment_refs": "Commitment",
        "retained_commitment_refs": "Commitment",
        "output_view_ref": "MemoryView",
    },
    ComputeSink: {"capability_ref": "CapabilityEnvelope"},
    ContextSnapshot: {
        "identity_ref": "AgentIdentity",
        "intent_ref": "Intent",
        "audience_ref": "AudienceSnapshot",
        "episode_ref": "ConversationEpisode",
        "selected_fragment_refs": "ContentFragment",
        "active_view_refs": "MemoryView",
        "producer_coverage_ref": "ProducerCoverage",
        "inherited_commitment_refs": "Commitment",
        "version_vector": ANY_TARGET,
        "sink_ref": "ComputeSink",
    },
    ModelCallEnvelope: {
        "snapshot_ref": "ContextSnapshot",
        "supplied_fragment_refs": "ContentFragment",
        "supplied_view_refs": "MemoryView",
        "sink_ref": "ComputeSink",
    },
    PerceptionSignals: {"event_refs": "InboundEvent", "window_refs": "InboundEvent"},
    Effect: {"target_ref": "ContentFragment", "capability_ref": "CapabilityEnvelope"},
    EffectPlan: {
        "intent_ref": "Intent",
        "snapshot_ref": "ContextSnapshot",
        "declared_fact_refs": "ContentFragment",
        "observed_target_refs": "InboundEvent",
        "producer_coverage_ref": "ProducerCoverage",
    },
    ReviewRequirement: {
        "relevant_commitment_refs": "Commitment",
        "sink_ref": "ComputeSink",
        "policy_version_vector": ANY_TARGET,
    },
    ReviewAssessment: {
        "supplied_view_refs": "MemoryView",
        "policy_version_vector": ANY_TARGET,
    },
    CommitDecision: {
        "authorization_ref": "CommitAuthorization",
        "assessment_ref": "ReviewAssessment",
    },
    CommitAuthorization: {
        "identity_ref": "AgentIdentity",
        "target_dependency_refs": ANY_TARGET,
        "authority_version_vector": ANY_TARGET,
        "policy_version_vector": ANY_TARGET,
        "capability_ref": "CapabilityEnvelope",
    },
    DispatchLease: {
        "authorization_ref": "CommitAuthorization",
        "reservation_ref": "DisclosureReservation",
    },
    DisclosureReservation: {"subject_resource_refs": "ContentFragment"},
    TurnOutcome: {"intent_ref": "Intent", "work_ref": "WorkItem", "effect_refs": "Effect"},
    ReviewEnvelope: {
        "plan_ref": "EffectPlan",
        "snapshot_ref": "ContextSnapshot",
        "audience_ref": "AudienceSnapshot",
        "intent_ref": "Intent",
        "relevant_policy_refs": POLICY_LIKE,
        "relevant_commitment_refs": "Commitment",
        "comparison_view_refs": "MemoryView",
        "bounded_quote_fragment_refs": "ContentFragment",
        "expected_coverage_refs": "ProducerCoverage",
        "sink_ref": "ComputeSink",
    },
}

#: Ref fields inside the embedded Provenance value object (handoff §4.1).
PROVENANCE_REF_TARGETS: dict[str, AllowedTargets] = {
    "producer_ref": "Principal",
    "origin_event_ref": "InboundEvent",
    "claimed_speaker_ref": "Principal",
    "custodian_refs": "Principal",
    "subject_refs": "Principal",
    "original_audience_ref": "AudienceSnapshot",
    "producer_coverage_ref": "ProducerCoverage",
    "model_claimed_parent_refs": "ContentFragment",
}

#: Ref fields inside the runtime-populated RuntimeOrigin value object.
RUNTIME_ORIGIN_REF_TARGETS: dict[str, AllowedTargets] = {
    "actor_ref": "Principal",
    "work_ref": "WorkItem",
    "snapshot_ref": "ContextSnapshot",
    "trigger_event_refs": "InboundEvent",
    "producer_coverage_ref": "ProducerCoverage",
}


def iter_ref_checks(record: Record) -> Iterator[tuple[str, RecordRef, AllowedTargets]]:
    """Yield (field path, ref, allowed targets) for every typed-ref field of
    the record, including refs nested in Provenance/RuntimeOrigin values."""
    for field, allowed in REF_TARGETS.get(type(record), {}).items():
        value = getattr(record, field)
        if value is None:
            continue
        refs = (value,) if isinstance(value, RecordRef) else value
        for ref in refs:
            yield field, ref, allowed
    provenance = getattr(record, "provenance", None)
    if isinstance(provenance, Provenance):
        yield from _value_object_checks(provenance, PROVENANCE_REF_TARGETS, "provenance")
    origin = getattr(record, "runtime_origin", None)
    if isinstance(origin, RuntimeOrigin):
        yield from _value_object_checks(origin, RUNTIME_ORIGIN_REF_TARGETS, "runtime_origin")


def _value_object_checks(
    value_object: Provenance | RuntimeOrigin,
    targets: dict[str, AllowedTargets],
    prefix: str,
) -> Iterator[tuple[str, RecordRef, AllowedTargets]]:
    for field, allowed in targets.items():
        value = getattr(value_object, field)
        if value is None:
            continue
        refs = (value,) if isinstance(value, RecordRef) else value
        for ref in refs:
            yield f"{prefix}.{field}", ref, allowed
