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
from dataclasses import dataclass, replace
from typing import Any

from agora.contracts import (
    AccessStatus,
    AssertionStatus,
    AudienceSnapshot,
    AuthorityBinding,
    AuthorityDomain,
    Commitment,
    CommitmentStrength,
    ContentFragment,
    ContextSnapshot,
    ConversationEpisode,
    DependencyKind,
    Derivation,
    DerivationState,
    EligibilityStatus,
    EnvelopePurpose,
    ExposureClass,
    FragmentKind,
    InboundEvent,
    InstructionAuthority,
    Integrity,
    IntendedUse,
    Intent,
    MemoryAccessRequest,
    MemoryAccessResult,
    MemoryRecord,
    MemoryStage,
    MemoryStatus,
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
    RuntimeOrigin,
    ScopeKind,
    SnapshotStatus,
    SourceKind,
    TransformCertificate,
    UseAuthorization,
    ValidationError,
    ViewKind,
    canonical_json_bytes,
    strict_json_loads,
)
from agora.ports import CrashCheckpoint, FaultInjector, VirtualClock
from agora.stores import CoreStore
from agora.transforms import TRANSFORM_REGISTRY, discharged_by, transform_spec

MAX_SELECTED_EVENTS = 20
MAX_CONTEXT_BYTES = 16 * 1024

_STAGE_ORDER: dict[MemoryStage, int] = {
    MemoryStage.EPHEMERAL: 0,
    MemoryStage.SOCIAL: 1,
    MemoryStage.CANDIDATE: 2,
    MemoryStage.DURABLE: 3,
}


class MemoryStateError(Exception):
    """Typed memory-lifecycle failure with a registry reason code."""

    def __init__(self, code: ReasonCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code


class ViewAdmissionError(Exception):
    """Structural view-admission failure at snapshot build (task 3.3): the
    containment decision happens here — never at dispatch, and it is not a
    reviewer-waivable exception."""

    def __init__(self, code: ReasonCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code


def _vector_cites(vector: tuple[RecordRef, ...], record_id: str, revision: int) -> bool:
    return any(ref.id == record_id and ref.revision == revision for ref in vector)


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
        if not eligible:
            # A policy with no configured eligible issuers can never meet
            # quorum; ALL_LISTED would otherwise satisfy both the quorum and
            # the chain-link check vacuously (review: no default-allow).
            return _verdict(AccessStatus.PENDING_AUTHORITY, ReasonCode.AUTHORITY_MISSING)
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
                return self._granted(case, authorization, policy, qualifying)
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
        if not distinct_issuers:
            return False  # empty chain links nothing (no default allow)
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
            # The transform's OUTPUT precision is bounded by the same
            # ceiling: an ABSTRACT-max authorization must not yield a
            # FIELDS projection (exact values above the ceiling).
            spec = TRANSFORM_REGISTRY.get(case.transform_id)
            if spec is not None and (
                _PRECISION_RANK[spec.output_precision]
                > _PRECISION_RANK[authorization.maximum_precision]
            ):
                return ReasonCode.VIEW_FORBIDDEN
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
        qualifying: list[AuthorityBinding],
    ) -> AccessVerdict:
        permitted_uses = tuple(
            dict.fromkeys(
                _USE_PERMISSION_FROM_OPERATION[operation] for operation in authorization.operations
            )
        )
        view_kind = _VIEW_KIND_FOR_PRECISION[case.requested_precision]
        cited = {ref.id for ref in authorization.binding_refs}
        return _verdict(
            AccessStatus.GRANTED,
            view_kind=view_kind,
            authorizations=(authorization,),
            # Only qualifying (quorum-meeting, valid, right-domain) bindings
            # the authorization actually cites back the grant — never
            # expired/revoked ones that happen to share the citation list.
            bindings=tuple(dict.fromkeys(binding for binding in qualifying if binding.id in cited)),
            inherited_commitments=self._attached_commitments(case, policy),
            permitted_uses=permitted_uses,
        )

    # -- data-class requests ---------------------------------------------------

    def evaluate_class(self, cases: tuple[EvaluationInput, ...]) -> AccessVerdict:
        """A data-class request evaluates per member resource; the most-closed
        verdict any member produces is the verdict. When every member grants,
        the merged verdict carries the union of all members' authorizations,
        bindings, and inherited commitments, and the intersection of
        permitted uses — a member's restrictions can never be dropped from
        the class view its fragment ships in (review MAJOR)."""
        if not cases:
            return _verdict(AccessStatus.DENIED, ReasonCode.POLICY_FORBIDDEN)
        verdicts = [self.evaluate(case) for case in cases]
        worst = max(verdicts, key=lambda verdict: _STATUS_PRECEDENCE[verdict.status])
        if worst.status != AccessStatus.GRANTED:
            return worst
        uses = tuple(dict.fromkeys(use for verdict in verdicts for use in verdict.permitted_uses))
        for verdict in verdicts:
            uses = tuple(use for use in uses if use in verdict.permitted_uses)
        return AccessVerdict(
            status=AccessStatus.GRANTED,
            reason_codes=(),
            view_kind=worst.view_kind,
            authorizations=tuple(
                dict.fromkeys(auth for verdict in verdicts for auth in verdict.authorizations)
            ),
            bindings=tuple(
                dict.fromkeys(binding for verdict in verdicts for binding in verdict.bindings)
            ),
            inherited_commitments=tuple(
                dict.fromkeys(
                    commitment
                    for verdict in verdicts
                    for commitment in verdict.inherited_commitments
                )
            ),
            permitted_uses=uses,
        )


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
            # One case per MEMBER RESOURCE (all its policies together), not
            # per policy — duplicate active policies on one resource must
            # surface the CONFLICT verdict on the class path too (review).
            grouped: dict[str, list[ResourcePolicy]] = {}
            for policy in all_policies:
                if policy.data_class == request.data_class:
                    grouped.setdefault(policy.resource_id, []).append(policy)
            cases = tuple(
                EvaluationInput(
                    intended_use=request.intended_use,
                    requested_precision=request.requested_precision,
                    transform_id=request.transform_id,
                    intent=intent if isinstance(intent, Intent) else None,
                    audience=audience if isinstance(audience, AudienceSnapshot) else None,
                    resource=self._fragment_or_none(resource_id),
                    policies=tuple(sorted(policies, key=lambda p: p.id)),
                    bindings=all_bindings,
                    authorizations=all_authorizations,
                    commitments=all_commitments,
                    now=now,
                )
                for resource_id, policies in sorted(grouped.items())
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
            # With several merged authorizations (data-class grants) the
            # bounds are the INTERSECTION: the view is only valid where
            # every granting member allows it (fail-closed).
            bounds_sets = [set(auth.audience_class_bounds) for auth in verdict.authorizations]
            audience_bounds = tuple(sorted(set.intersection(*bounds_sets))) if bounds_sets else ()
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
            try:
                view, certificate, transform_records = self._apply_transform(
                    request, view, verdict, now
                )
            except ValueError, ValidationError:
                # Authority granted but the transform cannot run on this
                # input (multi-member source, non-JSON payload, missing
                # parameters): a typed no-data result, never an aborted
                # turn with a dangling request (review).
                view = None
                certificate = None
                transform_records = ()
                source_view = None
                verdict = _verdict(AccessStatus.DENIED, ReasonCode.UNSUPPORTED)
                granted = False
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
            # Replay-dedup: records re-insert byte-identically, but the
            # audit log has no natural key — skip the row if the same
            # denial was already audited for this result (review).
            already = self.core._conn.execute(
                "SELECT 1 FROM core_audit WHERE entity_ref = ? AND reason_code = ? LIMIT 1",
                (result.id, verdict.reason_codes[0].value),
            ).fetchone()
            if already is None:
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

    def eligible_episode_fragments(
        self, episode_ref: RecordRef
    ) -> tuple[tuple[RecordRef, ...], int]:
        """Fragments of the episode's member events, newest last, and the
        bytes they consumed. Membership lives on
        ``ConversationEpisode.member_event_refs`` (handoff §5), maintained
        at ingest — members confer no scope rights, they only narrow
        context, so a foreign episode's fragments can never enter this
        snapshot. The episode is read at its pinned revision (as-of-turn
        membership); append order is journal order, so the newest
        MAX_SELECTED_EVENTS members are the episode's latest events, and
        the budget is SHARED with memory fragments (handoff §6.4: one
        16 KiB serialized social-content limit)."""
        episode = self.core.get_record(episode_ref.id, episode_ref.revision)
        if not isinstance(episode, ConversationEpisode):
            return (), 0
        members = episode.member_event_refs[-MAX_SELECTED_EVENTS:]
        selected: list[RecordRef] = []
        total = 0
        for member in reversed(members):
            event_record = self.core.get_record(member.id, member.revision)
            if not isinstance(event_record, InboundEvent):
                continue
            for ref in event_record.fragment_refs:
                fragment_record = self.core.get_record(ref.id, ref.revision)
                if not isinstance(fragment_record, ContentFragment):
                    continue
                # size via the fragment's own payload ref — the previous
                # convention-based lookup never matched the runner's payload
                # ids, silently zeroing the budget (latent M1 bug, flushed
                # out by the section-3 review)
                size = self._payload_size(fragment_record.payload_ref)
                if total + size > MAX_CONTEXT_BYTES:
                    continue  # omit excess whole fragments, keep metadata
                total += size
                selected.append(ref)
        return tuple(reversed(selected)), total

    def _eligible_memory_fragments(
        self, destination_id: str, remaining_bytes: int
    ) -> tuple[RecordRef, ...]:
        """Eligible memory assertion fragments for one destination scope,
        within the REMAINING shared context byte budget. Eligibility is
        filtered on the scope index before any payload is read;
        deterministically in memory id order."""
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
            if fragment_record.eligibility_status != EligibilityStatus.ACTIVE:
                continue  # superseded/tombstoned assertions never re-enter
            size = self._payload_size(fragment_record.payload_ref)
            if total + size > remaining_bytes:
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

    # -- staged promotion and corrections (M2 task 3.2, §6.3) ---------------

    def propose_memory(
        self,
        *,
        assertion_text: str,
        source_refs: tuple[RecordRef, ...],
        requested_stage: MemoryStage,
        requested_scope_kind: ScopeKind,
        scope_ref: str,
        existing_memory_ref: RecordRef | None,
        runtime_origin: RuntimeOrigin,
    ) -> RecordRef:
        """Explicit PROPOSE_MEMORY (handoff §5, §6.4): retain an ordinary
        observation as a scoped memory, or promote an existing one. The
        runtime origin is coordinator-minted — the model never sets it — and
        lineage is verified against that origin's producer coverage: the
        model may only cite fragments the runtime actually supplied, so a
        forged or declared-parent-omitted citation is a typed
        INVALID_REFERENCE, never a silent write. Storage inherits every
        source commitment — the cited fragments' own plus any carried by the
        granted views that returned them (a request cannot discharge
        commitments) — and is refused when a STRONG commitment forbids
        internal use. Scope is never widened: an ordinary observation is
        retained in its source room (the turn's destination) or as the
        agent's own; BROAD needs a curated scope-change permission that
        does not exist in this slice, DURABLE needs an existing CANDIDATE,
        and promotions carry the existing scope unchanged."""
        if existing_memory_ref is not None:
            memory = self._current_memory_or_error(existing_memory_ref)
            if (requested_scope_kind, scope_ref) != (memory.scope_kind, memory.scope_ref):
                raise MemoryStateError(
                    ReasonCode.POLICY_FORBIDDEN,
                    "promotion cannot move scope; broad promotion needs a curated permission",
                )
            return self.stage_memory(existing_memory_ref, requested_stage)
        if requested_stage is MemoryStage.DURABLE:
            raise MemoryStateError(
                ReasonCode.POLICY_FORBIDDEN,
                "DURABLE requires an existing CANDIDATE memory and an explicit proposal",
            )
        if requested_scope_kind is ScopeKind.BROAD:
            raise MemoryStateError(
                ReasonCode.POLICY_FORBIDDEN,
                "BROAD scope requires an explicit curated scope-change permission",
            )
        self._check_memory_scope(requested_scope_kind, scope_ref, runtime_origin)
        supplied = self._runtime_supplied_refs(runtime_origin)
        for ref in source_refs:
            if ref not in supplied:
                raise MemoryStateError(
                    ReasonCode.INVALID_REFERENCE,
                    f"source {ref.id} was not supplied by the runtime context",
                )
        inherited: dict[str, RecordRef] = {}
        for ref in sorted(source_refs, key=lambda r: r.id):
            fragment = self.core.get_record(ref.id, ref.revision)
            assert isinstance(fragment, ContentFragment)
            for commitment_ref in fragment.commitment_refs:
                inherited[commitment_ref.id] = commitment_ref
        # A fragment obtained through a granted view carries that view's
        # inherited commitments too — the authorization chain's restrictions
        # (which may exceed the fragment's own) travel with the bytes, so
        # storage cannot launder them by citing only fragment-level
        # commitments (review finding on propose_memory inheritance).
        coverage = self._origin_coverage(runtime_origin)
        for view_ref in coverage.input_view_refs:
            view = self.core.get_record(view_ref.id, view_ref.revision)
            if not isinstance(view, MemoryView):
                continue
            if any(ref in view.returned_fragment_refs for ref in source_refs):
                for commitment_ref in view.inherited_commitment_refs:
                    inherited[commitment_ref.id] = commitment_ref
        for commitment_ref in sorted(inherited.values(), key=lambda r: r.id):
            commitment = self.core.get_record(commitment_ref.id, commitment_ref.revision)
            if (
                isinstance(commitment, Commitment)
                and commitment.strength == CommitmentStrength.STRONG
                and not commitment.internal_use_allowed
            ):
                raise MemoryStateError(
                    ReasonCode.POLICY_FORBIDDEN,
                    f"source {commitment_ref.id} forbids internal use; no storage policy matches",
                )
        now = self.clock.now_rfc3339()
        memory_id = (
            "memory:propose:"
            + hashlib.sha256(
                canonical_json_bytes(
                    {
                        "assertion": assertion_text,
                        "sources": [ref.to_payload() for ref in source_refs],
                        "stage": requested_stage.value,
                        "scope": [requested_scope_kind.value, scope_ref],
                    }
                )
            ).hexdigest()[:24]
        )
        existing = self.core.current_ref(memory_id)
        if existing is not None:
            # Crash replay re-proposes byte-identical content: the stored
            # revision is the answer, never a duplicate.
            return existing
        fragment_id = f"fragment:{memory_id}"
        self.core.put_payload(f"payload:{fragment_id}", assertion_text, owner_ref=memory_id)
        fragment = ContentFragment(
            id=fragment_id,
            revision=1,
            created_at=now,
            kind=FragmentKind.TEXT,
            payload_ref=f"payload:{fragment_id}",
            provenance=Provenance(
                producer_ref=runtime_origin.actor_ref,
                source_kind=SourceKind.MEMORY,
                origin_event_ref=None,
                claimed_speaker_ref=None,
                instruction_authority=InstructionAuthority.AGENT_STATE,
                integrity=Integrity.DERIVED,
                custodian_refs=(),
                subject_refs=(),
                original_audience_ref=None,
                assertion_status=AssertionStatus.AGENT_ADOPTED_PREFERENCE,
                producer_coverage_ref=runtime_origin.producer_coverage_ref,
                model_claimed_parent_refs=tuple(source_refs),
                occurred_at=now,
                observed_at=now,
            ),
            commitment_refs=tuple(sorted(inherited.values(), key=lambda r: r.id)),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
        memory = MemoryRecord(
            id=memory_id,
            revision=1,
            created_at=now,
            assertion_fragment_ref=RecordRef(id=fragment.id, revision=fragment.revision),
            stage=requested_stage,
            scope_kind=requested_scope_kind,
            scope_ref=scope_ref,
            custodian_refs=(),
            subject_refs=(),
            commitment_refs=tuple(sorted(inherited.values(), key=lambda r: r.id)),
            assertion_status=AssertionStatus.AGENT_ADOPTED_PREFERENCE,
            supersedes_ref=None,
            valid_until=None,
            status=MemoryStatus.ACTIVE,
        )
        self.core.put_record(fragment)
        self.core.promote_memory(memory, expiry_ms=None)
        return RecordRef(id=memory.id, revision=memory.revision)

    def _check_memory_scope(
        self, scope_kind: ScopeKind, scope_ref: str, runtime_origin: RuntimeOrigin
    ) -> None:
        """Scope is bound to the turn, not chosen freely (handoff §6.4:
        ordinary observations are retained 'in their source room'): an
        AGENT-scoped memory is the agent's own and always allowed; a
        ROOM-scoped memory must name the destination of the context the
        runtime actually supplied. RELATIONSHIP and PROJECT have no curated
        mapping in this slice and are refused rather than guessed."""
        if scope_kind is ScopeKind.AGENT:
            return
        if scope_kind is not ScopeKind.ROOM:
            raise MemoryStateError(
                ReasonCode.POLICY_FORBIDDEN,
                f"{scope_kind.value} scope has no curated mapping in this slice",
            )
        snapshot = (
            self.core.get_record(
                runtime_origin.snapshot_ref.id, runtime_origin.snapshot_ref.revision
            )
            if runtime_origin.snapshot_ref is not None
            else None
        )
        if not isinstance(snapshot, ContextSnapshot):
            raise MemoryStateError(
                ReasonCode.INVALID_REFERENCE, "runtime origin cites a missing snapshot"
            )
        audience = self.core.get_record(snapshot.audience_ref.id, snapshot.audience_ref.revision)
        if not isinstance(audience, AudienceSnapshot) or audience.destination_id != scope_ref:
            raise MemoryStateError(
                ReasonCode.POLICY_FORBIDDEN,
                f"room scope must match the turn's destination ({scope_ref!r} does not)",
            )

    def _origin_coverage(self, runtime_origin: RuntimeOrigin) -> ProducerCoverage:
        if runtime_origin.producer_coverage_ref is None:
            raise MemoryStateError(
                ReasonCode.INVALID_REFERENCE, "runtime origin carries no producer coverage"
            )
        coverage = self.core.get_record(
            runtime_origin.producer_coverage_ref.id, runtime_origin.producer_coverage_ref.revision
        )
        if not isinstance(coverage, ProducerCoverage):
            raise MemoryStateError(
                ReasonCode.INVALID_REFERENCE, "runtime origin cites a missing producer coverage"
            )
        return coverage

    def _runtime_supplied_refs(self, runtime_origin: RuntimeOrigin) -> frozenset[RecordRef]:
        """The fragment refs the runtime actually put in front of the model:
        the producer coverage's input fragments plus every fragment an input
        view returned. Nothing else is citable lineage."""
        coverage = self._origin_coverage(runtime_origin)
        supplied = set(coverage.input_fragment_refs)
        for view_ref in coverage.input_view_refs:
            view = self.core.get_record(view_ref.id, view_ref.revision)
            if isinstance(view, MemoryView):
                supplied.update(view.returned_fragment_refs)
        return frozenset(supplied)

    def stage_memory(self, memory_ref: RecordRef, target_stage: MemoryStage) -> RecordRef:
        """Explicit staged promotion: EPHEMERAL → SOCIAL/CANDIDATE → DURABLE,
        always forward, never widening scope (scope is carried unchanged —
        no BROAD inference from DURABLE). Blocked with SUPERSEDED when the
        proposal cites a revision the memory has moved past."""
        memory = self._current_memory_or_error(memory_ref)
        if _STAGE_ORDER[target_stage] <= _STAGE_ORDER[memory.stage]:
            raise MemoryStateError(
                ReasonCode.POLICY_FORBIDDEN,
                f"promotion {memory.stage.value} -> {target_stage.value} does not advance",
            )
        if target_stage is MemoryStage.DURABLE and memory.stage is not MemoryStage.CANDIDATE:
            # §6.4: DURABLE promotion requires an existing CANDIDATE
            # revision — no skipping the curation stage (review).
            raise MemoryStateError(
                ReasonCode.POLICY_FORBIDDEN,
                f"DURABLE promotion requires a CANDIDATE memory, got {memory.stage.value}",
            )
        promoted = MemoryRecord(
            id=memory.id,
            revision=memory.revision + 1,
            created_at=self.clock.now_rfc3339(),
            assertion_fragment_ref=memory.assertion_fragment_ref,
            stage=target_stage,
            scope_kind=memory.scope_kind,
            scope_ref=memory.scope_ref,
            custodian_refs=memory.custodian_refs,
            subject_refs=memory.subject_refs,
            commitment_refs=memory.commitment_refs,
            assertion_status=memory.assertion_status,
            supersedes_ref=None,
            valid_until=memory.valid_until,
            status=MemoryStatus.ACTIVE,
        )
        self.core.promote_memory(
            promoted, expiry_ms=self._memory_expiry(memory.id, memory.revision)
        )
        return RecordRef(id=promoted.id, revision=promoted.revision)

    def correct_memory(self, memory_ref: RecordRef, corrected_text: str) -> RecordRef:
        """Correction: a new assertion revision supersedes the cited one
        (same stage/scope — corrections do not move scope either), and
        everything derived from the superseded assertion is invalidated via
        the dependency graph: derivations flip STALE, their retained output
        memories flip STALE (payload ineligible through the scope index),
        and affected snapshots flip INVALID."""
        memory = self._current_memory_or_error(memory_ref)
        now = self.clock.now_rfc3339()
        fragment_id = f"fragment:memory:{memory.id}:{memory.revision + 1}"
        self.core.put_payload(f"payload:{fragment_id}", corrected_text, owner_ref=memory.id)
        self.core.put_record(
            ContentFragment(
                id=fragment_id,
                revision=1,
                created_at=now,
                kind=FragmentKind.TEXT,
                payload_ref=f"payload:{fragment_id}",
                provenance=Provenance(
                    producer_ref=RecordRef(id="principal:agent", revision=1),
                    source_kind=SourceKind.MEMORY,
                    origin_event_ref=None,
                    claimed_speaker_ref=None,
                    instruction_authority=InstructionAuthority.AGENT_STATE,
                    integrity=Integrity.DERIVED,
                    custodian_refs=(),
                    subject_refs=(),
                    original_audience_ref=None,
                    assertion_status=AssertionStatus.OBSERVED_STATEMENT,
                    producer_coverage_ref=None,
                    model_claimed_parent_refs=(),
                    occurred_at=now,
                    observed_at=now,
                ),
                commitment_refs=memory.commitment_refs,
                eligibility_status=EligibilityStatus.ACTIVE,
            )
        )
        corrected = MemoryRecord(
            id=memory.id,
            revision=memory.revision + 1,
            created_at=now,
            assertion_fragment_ref=RecordRef(id=fragment_id, revision=1),
            stage=memory.stage,
            scope_kind=memory.scope_kind,
            scope_ref=memory.scope_ref,
            custodian_refs=memory.custodian_refs,
            subject_refs=memory.subject_refs,
            commitment_refs=memory.commitment_refs,
            assertion_status=memory.assertion_status,
            supersedes_ref=RecordRef(id=memory.id, revision=memory.revision),
            valid_until=memory.valid_until,
            status=MemoryStatus.ACTIVE,
        )
        self.core.promote_memory(
            corrected, expiry_ms=self._memory_expiry(memory.id, memory.revision)
        )
        self._invalidate_dependents(
            memory.assertion_fragment_ref.id, memory.assertion_fragment_ref.revision, now
        )
        return RecordRef(id=corrected.id, revision=corrected.revision)

    def _current_memory_or_error(self, memory_ref: RecordRef) -> MemoryRecord:
        current = self.core.current_ref(memory_ref.id)
        if current is None:
            raise MemoryStateError(
                ReasonCode.INVALID_REFERENCE, f"memory {memory_ref.id} does not exist"
            )
        if current.revision != memory_ref.revision:
            raise MemoryStateError(
                ReasonCode.SUPERSEDED,
                f"memory {memory_ref.id} is at revision {current.revision},"
                f" proposal cites {memory_ref.revision}",
            )
        record = self.core.get_record(memory_ref.id, memory_ref.revision)
        if not isinstance(record, MemoryRecord):
            raise MemoryStateError(ReasonCode.INVALID_REFERENCE, f"{memory_ref.id} is not a memory")
        if record.status != MemoryStatus.ACTIVE:
            # Invalidated (STALE) or removed (TOMBSTONED) memories cannot be
            # promoted or corrected back to relevance (review: invalidation
            # bypass through the module's own public API).
            raise MemoryStateError(
                ReasonCode.POLICY_FORBIDDEN,
                f"memory {memory_ref.id} is {record.status.value}: invalidated content"
                " cannot be promoted or corrected",
            )
        return record

    def _memory_expiry(self, memory_id: str, revision: int) -> int | None:
        rows = self.core.memory_scope_rows(memory_id)
        for row in rows:
            if row["revision"] == revision:
                return row["expiry_ms"]
        return None

    def _invalidate_dependents(self, fragment_id: str, fragment_revision: int, now: str) -> None:
        """Corrections invalidate what was derived from the superseded
        assertion, transitively: a summary-of-a-summary chain propagates
        all the way down (source → summary A → summary B → snapshot), not
        one level (imi feedback on task 3). Only dependents whose captured
        version actually matches are affected; flipped revisions carry the
        correction time as their creation time."""
        self._invalidate_from(fragment_id, fragment_revision, now, set())

    def _invalidate_from(
        self, fragment_id: str, fragment_revision: int, now: str, visited: set[str]
    ) -> None:
        if fragment_id in visited:
            return
        visited.add(fragment_id)
        for child_id in self.core.children_of(fragment_id):
            record = self.core.get_record(child_id)
            if isinstance(record, Derivation):
                if not _vector_cites(record.input_version_vector, fragment_id, fragment_revision):
                    continue
                self.core.put_record(
                    replace(
                        record,
                        revision=record.revision + 1,
                        created_at=now,
                        state=DerivationState.STALE,
                    )
                )
                self._stale_output_memory(record.output_fragment_ref, now)
                # whatever was derived from THIS derivation's output is
                # built on stale content too — keep walking the graph
                self._invalidate_from(
                    record.output_fragment_ref.id,
                    record.output_fragment_ref.revision,
                    now,
                    visited,
                )
            elif isinstance(record, ContextSnapshot):
                if not _vector_cites(record.version_vector, fragment_id, fragment_revision):
                    continue
                self.core.put_record(
                    replace(
                        record,
                        revision=record.revision + 1,
                        created_at=now,
                        status=SnapshotStatus.INVALID,
                    )
                )

    def _stale_output_memory(self, output_fragment_ref: RecordRef, now: str) -> None:
        """A derivation's retained output memory becomes ineligible: the
        scope-index row flips STALE with a new memory revision."""
        rows = self.core._conn.execute(
            "SELECT DISTINCT memory_id FROM core_memory_scope WHERE assertion_ref = ?",
            (output_fragment_ref.id,),
        ).fetchall()
        for row in rows:
            current = self.core.current_ref(row["memory_id"])
            record = self.core.get_record(row["memory_id"], current.revision) if current else None
            if not isinstance(record, MemoryRecord) or record.status == MemoryStatus.STALE:
                continue
            self.core.promote_memory(
                replace(
                    record,
                    revision=record.revision + 1,
                    created_at=now,
                    status=MemoryStatus.STALE,
                ),
                expiry_ms=self._memory_expiry(record.id, record.revision),
            )

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
        active_view_refs: tuple[RecordRef, ...] = (),
    ) -> ContextSnapshot:
        """Assemble + persist a context snapshot with CONTEXT_ASSEMBLY
        coverage; the snapshot's restrictions derive from actual inputs.

        Admitted views are checked structurally at build time (task 3.3): a
        view whose permitted uses are internal-only — whether backed by a
        STRONG commitment or constructed so by a transform — can never enter
        an externally effect-capable snapshot, a DM included; a view is also
        bound to the audience and purpose it was granted for. The failure is
        here, never deferred to dispatch, and it is not reviewer-waivable."""
        audience = self.core.get_record(audience_ref.id, audience_ref.revision)
        intent = self.core.get_record(intent_ref.id, intent_ref.revision)
        admitted = self._admit_views(
            active_view_refs,
            effect_capable=effect_capable,
            audience=audience if isinstance(audience, AudienceSnapshot) else None,
            intent=intent if isinstance(intent, Intent) else None,
        )
        episode, used_bytes = (
            self.eligible_episode_fragments(episode_ref) if episode_ref else ((), 0)
        )
        memories = (
            self._eligible_memory_fragments(audience.destination_id, MAX_CONTEXT_BYTES - used_bytes)
            if isinstance(audience, AudienceSnapshot)
            else ()
        )
        selected = tuple(dict.fromkeys((*episode, *memories)))
        inherited = tuple(
            dict.fromkeys(ref for view in admitted for ref in view.inherited_commitment_refs)
        )
        admitted_refs = tuple(RecordRef(id=view.id, revision=view.revision) for view in admitted)
        coverage = ProducerCoverage(
            id=f"coverage:context:{call_id}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            producer_kind=ProducerKind.CONTEXT_ASSEMBLY,
            input_fragment_refs=selected,
            input_view_refs=admitted_refs,
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
            active_view_refs=admitted_refs,
            producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
            exposure_class=ExposureClass.ORDINARY,
            inherited_commitment_refs=inherited,
            version_vector=tuple(sorted(selected, key=lambda ref: ref.id)),
            sink_ref=sink_ref,
            effect_capable=effect_capable,
            serializer_version=self.serializer_version,
            status=SnapshotStatus.ACTIVE,
        )
        self.core.put_record(snapshot)
        for ref in (*selected, *admitted_refs):
            self.core.add_dependency(
                RecordRef(id=snapshot.id, revision=snapshot.revision),
                ref,
                DependencyKind.PRODUCER_INPUT,
            )
        return snapshot

    def _admit_views(
        self,
        active_view_refs: tuple[RecordRef, ...],
        *,
        effect_capable: bool,
        audience: AudienceSnapshot | None,
        intent: Intent | None,
    ) -> tuple[MemoryView, ...]:
        """Load and structurally admit the requested views into THIS
        context. Failures: a missing view, a non-current citation, a
        non-ACTIVE status, or an expired valid_until → STALE_CONTEXT; an
        internal-only view against an externally effect-capable snapshot,
        an audience the view's bounds do not cover, or an intent whose
        purpose class the granting authorizations no longer cover →
        VIEW_FORBIDDEN (task 3.3 + imi feedback: a view is bound to the
        context it was granted for — it cannot be carried into another
        room, a DM, or a different purpose because its bytes exist)."""
        admitted: list[MemoryView] = []
        for view_ref in active_view_refs:
            record = self.core.get_record(view_ref.id, view_ref.revision)
            if not isinstance(record, MemoryView):
                raise ViewAdmissionError(
                    ReasonCode.STALE_CONTEXT, f"view {view_ref.id} does not resolve"
                )
            current = self.core.current_ref(view_ref.id)
            if current is None or current.revision != view_ref.revision:
                raise ViewAdmissionError(
                    ReasonCode.STALE_CONTEXT,
                    f"view {view_ref.id} cited at revision {view_ref.revision} is not current",
                )
            if record.status != RecordStatus.ACTIVE:
                raise ViewAdmissionError(
                    ReasonCode.STALE_CONTEXT, f"view {view_ref.id} is not active"
                )
            now = self.clock.now_rfc3339()
            if record.valid_until is not None and record.valid_until < now:
                raise ViewAdmissionError(
                    ReasonCode.STALE_CONTEXT, f"view {view_ref.id} has expired"
                )
            if effect_capable and all(
                use == IntendedUse.INTERNAL_ONLY for use in record.permitted_uses
            ):
                raise ViewAdmissionError(
                    ReasonCode.VIEW_FORBIDDEN,
                    f"strongly internal-only view {view_ref.id} cannot enter an"
                    " externally effect-capable snapshot (including a DM)",
                )
            if audience is not None and audience.destination_id not in record.audience_bounds:
                raise ViewAdmissionError(
                    ReasonCode.VIEW_FORBIDDEN,
                    f"view {view_ref.id} is not valid for audience {audience.destination_id}",
                )
            if intent is not None and record.authorization_refs:
                for auth_ref in record.authorization_refs:
                    authorization = self.core.get_record(auth_ref.id, auth_ref.revision)
                    if isinstance(authorization, UseAuthorization) and (
                        intent.purpose_class not in authorization.permitted_purpose_classes
                    ):
                        raise ViewAdmissionError(
                            ReasonCode.VIEW_FORBIDDEN,
                            f"view {view_ref.id} was granted for purpose class"
                            f" {authorization.permitted_purpose_classes}; the"
                            f" entering intent is {intent.purpose_class!r}",
                        )
            admitted.append(record)
        return tuple(admitted)

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
