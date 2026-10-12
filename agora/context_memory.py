"""Context and memory assembly for the replay slice (handoff §2, §5, §4.2).

ContextMemory builds audience/scoped context snapshots with runtime
producer coverage, records model-call envelopes with out-of-line message
payloads, and persists step coverage for every accepted model output. It
owns no sending and no agent personality.

M2 adds the access/use distinction: a pure ``AuthorityEvaluator`` (records
in, verdict out — no DB, no clock) implements the fixed fail-closed
evaluation order, and the record-building shell (``request_access``) loads
registries, evaluates, and persists MemoryAccessResult/MemoryView records.

Intended-use → operation mapping (this slice): INTERNAL_ONLY → READ, plus
TRANSFORM when the request names a transform; OBSERVABLE_INFLUENCE →
OBSERVABLE_INFLUENCE; EXPRESSION → EXPRESS. REVIEW and STORE are not
reachable from a MemoryAccessRequest. READ/TRANSFORM require RESOURCE_USE
bindings; EXPRESS/OBSERVABLE_INFLUENCE require DISCLOSURE bindings. No
principal kind, ownership, custody, or purpose rationale upgrades
authority anywhere in this module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from agora.contracts import (
    AccessStatus,
    AssertionStatus,
    AudienceSnapshot,
    AuthorityBinding,
    AuthorityDomain,
    Commitment,
    ContentFragment,
    ContextSnapshot,
    DependencyKind,
    EligibilityStatus,
    EnvelopePurpose,
    ExposureClass,
    FragmentKind,
    InstructionAuthority,
    Integrity,
    IntendedUse,
    Intent,
    MemoryAccessRequest,
    MemoryAccessResult,
    MemoryRecord,
    MemoryStage,
    MemoryView,
    ModelCallEnvelope,
    Pair,
    ProducerCoverage,
    ProducerKind,
    Provenance,
    ReasonCode,
    Record,
    RecordRef,
    RecordStatus,
    RequestedPrecision,
    ResourceOperation,
    ResourcePolicy,
    ScopeKind,
    SnapshotStatus,
    SourceKind,
    TransformCertificate,
    UseAuthorization,
    ViewKind,
    canonical_json_bytes,
    strict_json_loads,
)
from agora.ports import CrashCheckpoint, FaultInjector, VirtualClock
from agora.stores import CoreStore
from agora.transforms import TRANSFORM_REGISTRY, discharged_by, transform_spec

MAX_SELECTED_EVENTS = 20
MAX_CONTEXT_BYTES = 16 * 1024

# ---------------------------------------------------------------------------
# Authority evaluation (M2 design D1/D2): pure core, fixed order, fail-closed
# ---------------------------------------------------------------------------

_PRECISION_RANK: dict[RequestedPrecision, int] = {
    RequestedPrecision.ABSTRACT: 1,
    RequestedPrecision.FIELDS: 2,
    RequestedPrecision.EXACT: 3,
}

_VIEW_KIND_FOR_PRECISION: dict[RequestedPrecision, ViewKind] = {
    RequestedPrecision.EXACT: ViewKind.FULL,
    RequestedPrecision.FIELDS: ViewKind.PARTIAL,
    RequestedPrecision.ABSTRACT: ViewKind.ABSTRACTED,
}

_USE_TO_OPERATIONS: dict[IntendedUse, tuple[ResourceOperation, ...]] = {
    IntendedUse.INTERNAL_ONLY: (ResourceOperation.READ,),
    IntendedUse.OBSERVABLE_INFLUENCE: (ResourceOperation.OBSERVABLE_INFLUENCE,),
    IntendedUse.EXPRESSION: (ResourceOperation.EXPRESS,),
}

_REQUIRED_DOMAIN: dict[ResourceOperation, AuthorityDomain] = {
    ResourceOperation.READ: AuthorityDomain.RESOURCE_USE,
    ResourceOperation.TRANSFORM: AuthorityDomain.RESOURCE_USE,
    ResourceOperation.REVIEW: AuthorityDomain.RESOURCE_USE,
    ResourceOperation.STORE: AuthorityDomain.RESOURCE_USE,
    ResourceOperation.EXPRESS: AuthorityDomain.DISCLOSURE,
    ResourceOperation.OBSERVABLE_INFLUENCE: AuthorityDomain.DISCLOSURE,
}

_USE_PERMISSION_FROM_OPERATION: dict[ResourceOperation, IntendedUse] = {
    ResourceOperation.READ: IntendedUse.INTERNAL_ONLY,
    ResourceOperation.TRANSFORM: IntendedUse.INTERNAL_ONLY,
    ResourceOperation.REVIEW: IntendedUse.INTERNAL_ONLY,
    ResourceOperation.STORE: IntendedUse.INTERNAL_ONLY,
    ResourceOperation.EXPRESS: IntendedUse.EXPRESSION,
    ResourceOperation.OBSERVABLE_INFLUENCE: IntendedUse.OBSERVABLE_INFLUENCE,
}

#: Worst-case-wins precedence when a data-class request spans several
#: resources: the most-closed verdict any member produces is the verdict.
_STATUS_PRECEDENCE: dict[AccessStatus, int] = {
    AccessStatus.GRANTED: 0,
    AccessStatus.UNAVAILABLE: 1,
    AccessStatus.PENDING_AUTHORITY: 2,
    AccessStatus.DENIED: 3,
    AccessStatus.CONFLICT: 4,
}


@dataclass(frozen=True, slots=True)
class EvaluationInput:
    """All registry records one evaluation needs, loaded by the shell.
    ``policies`` must already be scoped to the request's resource (or, for
    a data-class request, to one member resource per case)."""

    intended_use: IntendedUse
    requested_precision: RequestedPrecision
    transform_id: str | None
    intent: Intent | None
    audience: AudienceSnapshot | None
    resource: ContentFragment | None
    policies: tuple[ResourcePolicy, ...]
    bindings: tuple[AuthorityBinding, ...]
    authorizations: tuple[UseAuthorization, ...]
    commitments: tuple[Commitment, ...]
    now: str


@dataclass(frozen=True, slots=True)
class AccessVerdict:
    status: AccessStatus
    reason_codes: tuple[ReasonCode, ...]
    view_kind: ViewKind = ViewKind.NONE
    authorizations: tuple[UseAuthorization, ...] = ()
    bindings: tuple[AuthorityBinding, ...] = ()
    inherited_commitments: tuple[Commitment, ...] = ()
    permitted_uses: tuple[IntendedUse, ...] = ()


def _verdict(status: AccessStatus, *reason_codes: ReasonCode, **payload: Any) -> AccessVerdict:
    return AccessVerdict(status=status, reason_codes=tuple(reason_codes), **payload)


class AuthorityEvaluator:
    """Pure evaluator: records in, ``AccessVerdict`` out. No store, no
    clock — validity times are compared against ``input.now``.

    Fixed order (design D2), each step fail-closed:

    1. reference validation (intent/audience/resource resolve)
    2. registry lookup — no active policy covering the resource denies
       (registry-gated; there is no default allow)
    3. conflict detection — contradictory active policies, or active
       commitments that split allow/forbid on the requested use
    4. binding eligibility — active, eligible-issuer, required-domain
       bindings meet the approval rule's issuer quorum
    5. authorization dimension match — operation, purpose class (from the
       intent), audience bound, precision ceiling, transforms, validity
    6. view construction for GRANTED

    Every non-GRANTED verdict carries view NONE, no permitted uses, and no
    authorization/binding references.
    """

    def evaluate(self, case: EvaluationInput) -> AccessVerdict:
        if case.transform_id is not None and case.transform_id not in TRANSFORM_REGISTRY:
            return _verdict(AccessStatus.DENIED, ReasonCode.UNSUPPORTED)
        if case.resource is None:
            return _verdict(AccessStatus.UNAVAILABLE, ReasonCode.INVALID_REFERENCE)
        if case.intent is None or case.audience is None:
            return _verdict(AccessStatus.DENIED, ReasonCode.INVALID_REFERENCE)

        policies = tuple(p for p in case.policies if p.status.value == "ACTIVE")
        if not policies:
            return _verdict(AccessStatus.DENIED, ReasonCode.POLICY_FORBIDDEN)
        if len(policies) > 1:
            return _verdict(AccessStatus.CONFLICT, ReasonCode.AUTHORITY_CONFLICT)
        policy = policies[0]

        operations = self._operations(case)
        attached = self._attached_commitments(case, policy)
        conflict = self._commitment_conflict(attached, case.intended_use)
        if conflict:
            return _verdict(AccessStatus.CONFLICT, ReasonCode.AUTHORITY_CONFLICT)

        required_domains = {_REQUIRED_DOMAIN[op] for op in operations}
        eligible = {ref.id for ref in policy.eligible_issuer_refs}
        qualifying: list[AuthorityBinding] = []
        for domain in sorted(required_domains, key=lambda d: d.value):
            active = [
                binding
                for binding in case.bindings
                if binding.domain == domain
                and binding.resource_id == case.resource.id
                and binding.issuer_ref.id in eligible
                and binding.revoked_at is None
                and (binding.valid_until is None or binding.valid_until >= case.now)
            ]
            if policy.approval_rule.value == "SINGLE_ISSUER":
                if not active:
                    return _verdict(AccessStatus.PENDING_AUTHORITY, ReasonCode.AUTHORITY_MISSING)
                qualifying.extend(active[:1])
            else:  # ALL_LISTED: every configured issuer must be bound
                bound = {binding.issuer_ref.id for binding in active}
                missing = sorted(eligible - bound)
                if missing:
                    return _verdict(AccessStatus.PENDING_AUTHORITY, ReasonCode.AUTHORITY_MISSING)
                qualifying.extend(active)

        candidates = [
            authorization
            for authorization in case.authorizations
            if authorization.status.value == "ACTIVE"
            and authorization.resource_id == case.resource.id
            and (authorization.valid_until is None or authorization.valid_until >= case.now)
            and self._authorization_linked(authorization, qualifying, policy)
        ]
        if not candidates:
            # The authority chain resolved (bindings meet the quorum) but no
            # active, chain-linked use authorization covers this resource.
            return _verdict(AccessStatus.DENIED, ReasonCode.POLICY_FORBIDDEN)

        first_denial: ReasonCode | None = None
        for authorization in candidates:
            denial = self._dimension_failure(case, authorization, operations, policy)
            if denial is None:
                return self._granted(case, authorization, policy)
            if first_denial is None:
                first_denial = denial
        # No candidate grants: report the first candidate's failing dimension
        # deterministically (operation/purpose/audience/precision/transform).
        assert first_denial is not None
        return _verdict(AccessStatus.DENIED, first_denial)

    def _authorization_linked(
        self,
        authorization: UseAuthorization,
        qualifying: list[AuthorityBinding],
        policy: ResourcePolicy,
    ) -> bool:
        """An authorization grants only through the qualifying binding
        chain: its cited bindings and issuers must actually back it."""
        cited = {ref.id for ref in authorization.binding_refs}
        issuers = {ref.id for ref in authorization.issuer_refs}
        backed = [
            binding
            for binding in qualifying
            if binding.id in cited and binding.issuer_ref.id in issuers
        ]
        if policy.approval_rule.value == "SINGLE_ISSUER":
            return bool(backed)
        distinct_issuers = {binding.issuer_ref.id for binding in qualifying}
        return {binding.issuer_ref.id for binding in backed} == distinct_issuers

    # -- steps ----------------------------------------------------------------

    def _operations(self, case: EvaluationInput) -> tuple[ResourceOperation, ...]:
        operations = list(_USE_TO_OPERATIONS[case.intended_use])
        if case.transform_id is not None:
            operations.append(ResourceOperation.TRANSFORM)
        return tuple(operations)

    def _attached_commitments(
        self, case: EvaluationInput, policy: ResourcePolicy
    ) -> tuple[Commitment, ...]:
        by_id = {commitment.id: commitment for commitment in case.commitments}
        attached: dict[str, Commitment] = {}
        for ref in case.resource.commitment_refs if case.resource else ():
            if ref.id in by_id:
                attached[ref.id] = by_id[ref.id]
        for ref in policy.commitment_refs:
            if ref.id in by_id:
                attached[ref.id] = by_id[ref.id]
        return tuple(attached.values())

    def _commitment_conflict(self, commitments: tuple[Commitment, ...], use: IntendedUse) -> bool:
        """Contradictory active commitments both applying to the requested
        use: some permit the dimension and some forbid it."""
        allows = {
            {
                IntendedUse.INTERNAL_ONLY: commitment.internal_use_allowed,
                IntendedUse.OBSERVABLE_INFLUENCE: commitment.observable_influence_allowed,
                IntendedUse.EXPRESSION: commitment.expression_allowed,
            }[use]
            for commitment in commitments
        }
        return len(allows) == 2

    def _dimension_failure(
        self,
        case: EvaluationInput,
        authorization: UseAuthorization,
        operations: tuple[ResourceOperation, ...],
        policy: ResourcePolicy,
    ) -> ReasonCode | None:
        """None when this authorization covers every requested dimension
        (the granted shape); otherwise the first failing dimension's code.
        Dimension order is fixed and deterministic."""

        for operation in operations:
            if operation not in authorization.operations:
                return ReasonCode.POLICY_FORBIDDEN
        assert case.intent is not None and case.audience is not None
        if case.intent.purpose_class not in authorization.permitted_purpose_classes:
            return ReasonCode.POLICY_FORBIDDEN
        if case.audience.destination_id not in authorization.audience_class_bounds:
            return ReasonCode.POLICY_FORBIDDEN
        if (
            _PRECISION_RANK[case.requested_precision]
            > _PRECISION_RANK[authorization.maximum_precision]
        ):
            return ReasonCode.VIEW_FORBIDDEN
        if case.transform_id is not None:
            if case.transform_id not in authorization.allowed_transform_ids:
                return ReasonCode.UNSUPPORTED
            if case.transform_id not in policy.allowed_transform_ids:
                return ReasonCode.POLICY_FORBIDDEN
        return None

    def _granted(
        self,
        case: EvaluationInput,
        authorization: UseAuthorization,
        policy: ResourcePolicy,
    ) -> AccessVerdict:
        permitted_uses = tuple(
            dict.fromkeys(
                _USE_PERMISSION_FROM_OPERATION[operation] for operation in authorization.operations
            )
        )
        view_kind = _VIEW_KIND_FOR_PRECISION[case.requested_precision]
        return _verdict(
            AccessStatus.GRANTED,
            view_kind=view_kind,
            authorizations=(authorization,),
            bindings=tuple(
                dict.fromkeys(
                    binding
                    for binding in case.bindings
                    if any(ref.id == binding.id for ref in authorization.binding_refs)
                )
            ),
            inherited_commitments=self._attached_commitments(case, policy),
            permitted_uses=permitted_uses,
        )

    # -- data-class requests ---------------------------------------------------

    def evaluate_class(self, cases: tuple[EvaluationInput, ...]) -> AccessVerdict:
        """A data-class request evaluates per member resource; the most-closed
        verdict any member produces is the verdict (fail-closed combine)."""
        if not cases:
            return _verdict(AccessStatus.DENIED, ReasonCode.POLICY_FORBIDDEN)
        verdicts = [self.evaluate(case) for case in cases]
        worst = max(verdicts, key=lambda verdict: _STATUS_PRECEDENCE[verdict.status])
        return worst


class ContextMemory:
    def __init__(
        self,
        core: CoreStore,
        clock: VirtualClock,
        injector: FaultInjector | None = None,
        *,
        serializer_version: str = "ser:v1",
    ) -> None:
        self.core = core
        self.clock = clock
        self.injector = injector
        self.serializer_version = serializer_version

    def _trip(self, checkpoint: CrashCheckpoint) -> None:
        if self.injector is not None:
            self.injector.trip(checkpoint)

    # -- protected access (M2 design D1: the record-building shell) ---------

    def _current_of[R: Record](self, cls: type[R]) -> tuple[R, ...]:
        """Current revisions of one record type, in id order (deterministic)."""
        rows = self.core._conn.execute(
            "SELECT record_id, revision FROM core_records WHERE record_type = ?"
            " AND is_current = 1 ORDER BY record_id",
            (cls.TYPE_NAME,),
        ).fetchall()
        return tuple(
            record
            for row in rows
            if isinstance(record := self.core.get_record(row["record_id"], row["revision"]), cls)
        )

    def _fragment_or_none(self, record_id: str) -> ContentFragment | None:
        record = self.core.get_record(record_id)
        return record if isinstance(record, ContentFragment) else None

    def request_access(self, request: MemoryAccessRequest) -> MemoryAccessResult:
        """Persist the request (necessity text out-of-line), evaluate
        authority independently, and persist the typed result — plus, when
        granted, the restricted MemoryView citing its authorization chain
        and inherited commitments. The evaluation itself never sees payload
        bytes; a non-GRANTED result persists no view, no permitted uses,
        and no authorizations (handoff §5, D07)."""
        now = self.clock.now_rfc3339()
        self.core.put_record(request, text_field_refs={"necessity_text": "necessity_payload_ref"})
        intent = self.core.get_record(request.intent_ref.id, request.intent_ref.revision)
        audience = self.core.get_record(request.audience_ref.id, request.audience_ref.revision)
        all_policies = self._current_of(ResourcePolicy)
        all_bindings = self._current_of(AuthorityBinding)
        all_authorizations = self._current_of(UseAuthorization)
        all_commitments = self._current_of(Commitment)

        evaluator = AuthorityEvaluator()
        if request.resource_id is not None:
            cases = (
                EvaluationInput(
                    intended_use=request.intended_use,
                    requested_precision=request.requested_precision,
                    transform_id=request.transform_id,
                    intent=intent if isinstance(intent, Intent) else None,
                    audience=audience if isinstance(audience, AudienceSnapshot) else None,
                    resource=self._fragment_or_none(request.resource_id),
                    policies=tuple(p for p in all_policies if p.resource_id == request.resource_id),
                    bindings=all_bindings,
                    authorizations=all_authorizations,
                    commitments=all_commitments,
                    now=now,
                ),
            )
            class_resource_id = request.resource_id
        else:
            assert request.data_class is not None
            members = tuple(
                policy for policy in all_policies if policy.data_class == request.data_class
            )
            cases = tuple(
                EvaluationInput(
                    intended_use=request.intended_use,
                    requested_precision=request.requested_precision,
                    transform_id=request.transform_id,
                    intent=intent if isinstance(intent, Intent) else None,
                    audience=audience if isinstance(audience, AudienceSnapshot) else None,
                    resource=self._fragment_or_none(policy.resource_id),
                    policies=(policy,),
                    bindings=all_bindings,
                    authorizations=all_authorizations,
                    commitments=all_commitments,
                    now=now,
                )
                for policy in members
            )
            class_resource_id = request.data_class
        assert class_resource_id is not None
        verdict = (
            evaluator.evaluate(cases[0])
            if request.resource_id is not None
            else evaluator.evaluate_class(cases)
        )

        coverage = ProducerCoverage(
            id=f"coverage:access:{request.request_id}",
            revision=1,
            created_at=now,
            producer_kind=ProducerKind.CONTEXT_ASSEMBLY,
            input_fragment_refs=tuple(
                RecordRef(id=case.resource.id, revision=case.resource.revision)
                for case in cases
                if case.resource is not None
            ),
            input_view_refs=(),
            input_intent_refs=(request.intent_ref,),
            input_policy_refs=tuple(
                RecordRef(id=policy.id, revision=policy.revision)
                for case in cases
                for policy in case.policies
            ),
            sink_ref=None,
            call_id=f"access:{request.request_id}",
            runtime_epoch=self.core.runtime_epoch,
        )

        granted = verdict.status == AccessStatus.GRANTED
        view: MemoryView | None = None
        if granted:
            authorization_refs = tuple(
                RecordRef(id=auth.id, revision=auth.revision) for auth in verdict.authorizations
            )
            audience_bounds = tuple(
                sorted(
                    {
                        bound
                        for auth in verdict.authorizations
                        for bound in auth.audience_class_bounds
                    }
                )
            )
            valid_until = min(
                (
                    auth.valid_until
                    for auth in verdict.authorizations
                    if auth.valid_until is not None
                ),
                default=None,
            )
            view = MemoryView(
                id=f"view:{request.request_id}",
                revision=1,
                created_at=now,
                resource_id=class_resource_id,
                returned_fragment_refs=tuple(
                    RecordRef(id=case.resource.id, revision=case.resource.revision)
                    for case in cases
                    if case.resource is not None
                ),
                authorization_refs=authorization_refs,
                transform_certificate_ref=None,
                producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
                precision=request.requested_precision,
                inherited_commitment_refs=tuple(
                    RecordRef(id=c.id, revision=c.revision) for c in verdict.inherited_commitments
                ),
                permitted_uses=verdict.permitted_uses,
                audience_bounds=audience_bounds,
                valid_until=valid_until,
                status=RecordStatus.ACTIVE,
            )
        certificate: TransformCertificate | None = None
        transform_records: tuple[Record, ...] = ()
        source_view = view
        if granted and view is not None and request.transform_id is not None:
            view, certificate, transform_records = self._apply_transform(
                request, view, verdict, now
            )
        result = MemoryAccessResult(
            id=f"result:{request.request_id}",
            revision=1,
            created_at=now,
            request_id=request.request_id,
            access_status=verdict.status,
            view_kind=(
                _VIEW_KIND_FOR_PRECISION[view.precision] if view is not None else verdict.view_kind
            ),
            view_ref=RecordRef(id=view.id, revision=view.revision) if view else None,
            reason_codes=verdict.reason_codes,
            safe_feedback_template_id=None,
            permitted_uses=verdict.permitted_uses,
            producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
            transform_certificate_ref=(
                RecordRef(id=certificate.id, revision=certificate.revision) if certificate else None
            ),
            authorization_refs=tuple(
                RecordRef(id=auth.id, revision=auth.revision) for auth in verdict.authorizations
            )
            if granted
            else (),
            inherited_commitment_refs=tuple(
                RecordRef(id=c.id, revision=c.revision) for c in verdict.inherited_commitments
            )
            if granted
            else (),
        )
        batch: tuple[Record, ...] = (
            *((coverage, source_view) if source_view else (coverage,)),
            *transform_records,
            result,
        )
        self.core.put_records(batch)
        if not granted and verdict.reason_codes:
            self.core.audit(
                operation="access",
                entity_ref=result.id,
                reason_code=verdict.reason_codes[0].value,
                version_refs=(request.id,),
                at_ms=self.clock.now_ms(),
            )
        return result

    def _apply_transform(
        self,
        request: MemoryAccessRequest,
        source_view: MemoryView,
        verdict: AccessVerdict,
        now: str,
    ) -> tuple[MemoryView, TransformCertificate, tuple[Record, ...]]:
        """Run one registry transform over the authorized source view (design
        D3): compute the output out-of-line, write a TransformCertificate
        naming discharged vs retained commitments (only the spec's discharge
        rule names anything), and build the output view citing the
        certificate. Output views are internal-use only in this slice — a
        predicate may itself reveal finances, so no transform widens uses."""
        spec = transform_spec(request.transform_id) if request.transform_id else None
        if spec is None:
            raise ValueError("transform application requires a transform id")
        if len(source_view.returned_fragment_refs) != 1:
            raise ValueError("a transform applies to exactly one source resource")
        parameters = json.loads(self.core.get_meta("fixture_transforms") or "{}").get(
            spec.transform_id
        )
        if parameters is None:
            raise ValueError(f"no fixture parameters seeded for {spec.transform_id}")
        source_fragment = self._fragment_or_none(source_view.returned_fragment_refs[0].id)
        if source_fragment is None:
            raise ValueError("source fragment missing")
        payload_text = self.core.get_payload(source_fragment.payload_ref)
        if payload_text is None:
            raise ValueError("source payload unavailable")
        payload = strict_json_loads(payload_text)
        output = spec.run(payload, parameters)
        output_payload_id = f"payload:transform:{request.request_id}"
        self.core.put_payload(
            output_payload_id,
            canonical_json_bytes(output).decode("utf-8"),
            owner_ref=f"view:{request.request_id}",
        )

        inherited = verdict.inherited_commitments
        discharged = tuple(c for c in inherited if discharged_by(spec, c))
        retained = tuple(c for c in inherited if not discharged_by(spec, c))
        authorization_refs = tuple(
            RecordRef(id=auth.id, revision=auth.revision) for auth in verdict.authorizations
        )
        binding_refs = tuple(
            RecordRef(id=binding.id, revision=binding.revision) for binding in verdict.bindings
        )
        coverage = ProducerCoverage(
            id=f"coverage:transform:{request.request_id}",
            revision=1,
            created_at=now,
            producer_kind=ProducerKind.TRANSFORM,
            input_fragment_refs=(
                RecordRef(id=source_fragment.id, revision=source_fragment.revision),
            ),
            input_view_refs=(RecordRef(id=source_view.id, revision=source_view.revision),),
            input_intent_refs=(request.intent_ref,),
            input_policy_refs=source_view.authorization_refs,
            sink_ref=None,
            call_id=f"transform:{request.request_id}",
            runtime_epoch=self.core.runtime_epoch,
        )
        output_fragment = ContentFragment(
            id=f"fragment:transform:{request.request_id}",
            revision=1,
            created_at=now,
            kind=FragmentKind.TEXT,
            payload_ref=output_payload_id,
            provenance=Provenance(
                producer_ref=RecordRef(id="principal:agent", revision=1),
                source_kind=SourceKind.TOOL_RESULT,
                origin_event_ref=None,
                claimed_speaker_ref=None,
                instruction_authority=InstructionAuthority.AGENT_STATE,
                integrity=Integrity.DERIVED,
                custodian_refs=(),
                subject_refs=(),
                original_audience_ref=None,
                assertion_status=AssertionStatus.INFERENCE,
                producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
                model_claimed_parent_refs=(
                    RecordRef(id=source_fragment.id, revision=source_fragment.revision),
                ),
                occurred_at=now,
                observed_at=now,
            ),
            commitment_refs=tuple(RecordRef(id=c.id, revision=c.revision) for c in retained),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
        certificate = TransformCertificate(
            id=f"certificate:{request.request_id}",
            revision=1,
            created_at=now,
            registry_transform_id=spec.transform_id,
            parameter_values=tuple(
                Pair(first=name, second=value) for name, value in parameters.items()
            ),
            source_coverage_ref=source_view.producer_coverage_ref,
            authorizing_binding_refs=binding_refs,
            authorizing_use_refs=authorization_refs,
            discharged_commitment_refs=tuple(
                RecordRef(id=c.id, revision=c.revision) for c in discharged
            ),
            retained_commitment_refs=tuple(
                RecordRef(id=c.id, revision=c.revision) for c in retained
            ),
            output_view_ref=RecordRef(id=f"view:{request.request_id}:transform", revision=1),
        )
        output_view = MemoryView(
            id=f"view:{request.request_id}:transform",
            revision=1,
            created_at=now,
            resource_id=source_view.resource_id,
            returned_fragment_refs=(
                RecordRef(id=output_fragment.id, revision=output_fragment.revision),
            ),
            authorization_refs=authorization_refs,
            transform_certificate_ref=RecordRef(id=certificate.id, revision=certificate.revision),
            producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
            precision=spec.output_precision,
            inherited_commitment_refs=tuple(
                RecordRef(id=c.id, revision=c.revision) for c in retained
            ),
            permitted_uses=(IntendedUse.INTERNAL_ONLY,),
            audience_bounds=source_view.audience_bounds,
            valid_until=source_view.valid_until,
            status=RecordStatus.ACTIVE,
        )
        return output_view, certificate, (coverage, output_fragment, certificate, output_view)

    def eligible_episode_fragments(self, episode_ref: RecordRef) -> tuple[RecordRef, ...]:
        """Recent ingested fragments for an episode, newest last, within the
        fixture bounds (last 20 events, 16 KiB, whole-fragment omission)."""
        rows = self.core._conn.execute(
            "SELECT fragment_refs FROM core_ingestion ORDER BY journal_seq DESC LIMIT ?",
            (MAX_SELECTED_EVENTS,),
        ).fetchall()
        selected: list[RecordRef] = []
        total = 0
        for row in rows:
            for fragment_id in json.loads(row["fragment_refs"]):
                ref = self.core.current_ref(fragment_id)
                if ref is None:
                    continue
                size = self._payload_size(f"payload:fragment:{fragment_id}")
                if total + size > MAX_CONTEXT_BYTES:
                    continue  # omit excess whole fragments, keep metadata
                total += size
                selected.append(ref)
        return tuple(reversed(selected))

    def _eligible_memory_fragments(self, destination_id: str) -> tuple[RecordRef, ...]:
        """Eligible memory assertion fragments for one destination scope,
        within the shared context byte budget. Eligibility is filtered on
        the scope index before any payload is read; deterministically in
        memory id order."""
        memory_refs = self.core.eligible_memory_refs(
            scope_kind=ScopeKind.ROOM.value,
            scope_ref=destination_id,
            now_ms=self.clock.now_ms(),
            stages=frozenset({MemoryStage.SOCIAL, MemoryStage.CANDIDATE, MemoryStage.DURABLE}),
        )
        selected: list[RecordRef] = []
        total = 0
        for memory_ref in memory_refs:
            record = self.core.get_record(memory_ref.id, memory_ref.revision)
            if not isinstance(record, MemoryRecord):
                continue
            fragment_record = self.core.get_record(
                record.assertion_fragment_ref.id, record.assertion_fragment_ref.revision
            )
            if not isinstance(fragment_record, ContentFragment):
                continue
            size = self._payload_size(fragment_record.payload_ref)
            if total + size > MAX_CONTEXT_BYTES:
                continue  # omit excess whole fragments, keep metadata
            total += size
            selected.append(RecordRef(id=fragment_record.id, revision=fragment_record.revision))
        return tuple(selected)

    def _payload_size(self, payload_id: str) -> int:
        row = self.core._conn.execute(
            "SELECT length(data) AS n FROM core_payloads WHERE payload_id = ?",
            (payload_id,),
        ).fetchone()
        return row["n"] if row is not None else 0

    def build_snapshot(
        self,
        *,
        identity_ref: RecordRef,
        intent_ref: RecordRef,
        audience_ref: RecordRef,
        episode_ref: RecordRef | None,
        sink_ref: RecordRef,
        effect_capable: bool,
        call_id: str,
    ) -> ContextSnapshot:
        """Assemble + persist a context snapshot with CONTEXT_ASSEMBLY
        coverage; the snapshot's restrictions derive from actual inputs."""
        episode = self.eligible_episode_fragments(episode_ref) if episode_ref else ()
        audience = self.core.get_record(audience_ref.id, audience_ref.revision)
        memories = (
            self._eligible_memory_fragments(audience.destination_id)
            if isinstance(audience, AudienceSnapshot)
            else ()
        )
        selected = tuple(dict.fromkeys((*episode, *memories)))
        coverage = ProducerCoverage(
            id=f"coverage:context:{call_id}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            producer_kind=ProducerKind.CONTEXT_ASSEMBLY,
            input_fragment_refs=selected,
            input_view_refs=(),
            input_intent_refs=(intent_ref,),
            input_policy_refs=(),
            sink_ref=sink_ref,
            call_id=call_id,
            runtime_epoch=self.core.runtime_epoch,
        )
        self.core.put_record(coverage)
        snapshot = ContextSnapshot(
            id=f"snapshot:{call_id}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            identity_ref=identity_ref,
            intent_ref=intent_ref,
            audience_ref=audience_ref,
            episode_ref=episode_ref,
            selected_fragment_refs=selected,
            active_view_refs=(),
            producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
            exposure_class=ExposureClass.ORDINARY,
            inherited_commitment_refs=(),
            version_vector=tuple(sorted(selected, key=lambda ref: ref.id)),
            sink_ref=sink_ref,
            effect_capable=effect_capable,
            serializer_version=self.serializer_version,
            status=SnapshotStatus.ACTIVE,
        )
        self.core.put_record(snapshot)
        for ref in selected:
            self.core.add_dependency(
                RecordRef(id=snapshot.id, revision=snapshot.revision),
                ref,
                DependencyKind.PRODUCER_INPUT,
            )
        return snapshot

    def record_envelope(
        self,
        *,
        snapshot: ContextSnapshot,
        purpose: EnvelopePurpose,
        messages: list[dict[str, Any]],
        sink_ref: RecordRef,
        call_id: str,
    ) -> ModelCallEnvelope:
        """Persist the exact model-call envelope: the messages payload goes
        out-of-line; the envelope binds its digest and supplied refs."""
        payload_bytes = canonical_json_bytes(messages)
        payload_id = f"payload:messages:{call_id}"
        self.core.put_payload(
            payload_id,
            payload_bytes.decode("utf-8"),
            owner_ref=f"snapshot:{snapshot.id}",
        )
        envelope = ModelCallEnvelope(
            id=f"envelope:{call_id}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            snapshot_ref=RecordRef(id=snapshot.id, revision=snapshot.revision),
            purpose=purpose,
            exact_messages_payload_ref=payload_id,
            tool_schema_digest=_digest(b"no-tools:v1"),
            supplied_fragment_refs=snapshot.selected_fragment_refs,
            supplied_view_refs=snapshot.active_view_refs,
            sink_ref=sink_ref,
            request_digest=_digest(payload_bytes),
        )
        self.core.put_record(envelope)
        return envelope

    def record_step_coverage(
        self,
        *,
        snapshot: ContextSnapshot,
        envelope: ModelCallEnvelope,
        call_id: str,
    ) -> ProducerCoverage:
        """Every model output gets runtime producer coverage from the actual
        invocation inputs (handoff §4.3 / D05)."""
        coverage = ProducerCoverage(
            id=f"coverage:main:{call_id}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            producer_kind=ProducerKind.MAIN,
            input_fragment_refs=snapshot.selected_fragment_refs,
            input_view_refs=snapshot.active_view_refs,
            input_intent_refs=(snapshot.intent_ref,),
            input_policy_refs=(),
            sink_ref=envelope.sink_ref,
            call_id=call_id,
            runtime_epoch=self.core.runtime_epoch,
        )
        self.core.put_record(coverage)
        self.core.add_dependency(
            RecordRef(id=coverage.id, revision=coverage.revision),
            RecordRef(
                id=snapshot.producer_coverage_ref.id,
                revision=snapshot.producer_coverage_ref.revision,
            ),
            DependencyKind.PRODUCER_INPUT,
        )
        return coverage


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
