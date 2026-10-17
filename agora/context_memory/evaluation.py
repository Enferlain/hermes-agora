"""Authority evaluation (M2 design D1/D2): the pure, fixed-order,
fail-closed access engine behind REQUEST_ACCESS — bindings, commitments,
precision ranking and worst-case-wins precedence over the resources a
request spans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agora.contracts import (
    AccessStatus,
    AudienceSnapshot,
    AuthorityBinding,
    AuthorityDomain,
    Commitment,
    ContentFragment,
    IntendedUse,
    Intent,
    ReasonCode,
    RequestedPrecision,
    ResourceOperation,
    ResourcePolicy,
    UseAuthorization,
    ViewKind,
)
from agora.transforms import TRANSFORM_REGISTRY

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
