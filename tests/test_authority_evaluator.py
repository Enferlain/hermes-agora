"""M2 task 2.1: the pure AuthorityEvaluator battery.

Every access status, SINGLE_ISSUER vs ALL_LISTED quorum, domain-scoped
operations, dimension matching, and operator non-priority — all against
in-memory records, no store.
"""

from __future__ import annotations

from agora.context_memory import AuthorityEvaluator, EvaluationInput
from agora.contracts import (
    AccessStatus,
    ApprovalRule,
    AssertionStatus,
    AudienceSnapshot,
    AuthorityBinding,
    AuthorityDomain,
    Commitment,
    CommitmentStrength,
    Completeness,
    ContentFragment,
    EligibilityStatus,
    EnvironmentKind,
    FragmentKind,
    HistoryVisibility,
    InstructionAuthority,
    Integrity,
    IntendedUse,
    Intent,
    IntentState,
    PrincipalKind,
    Provenance,
    ReasonCode,
    RecordRef,
    RecordStatus,
    RequestedPrecision,
    ResourceOperation,
    ResourcePolicy,
    SourceKind,
    UseAuthorization,
    ViewKind,
    Visibility,
)

NOW = "2026-10-12T12:00:00Z"
HUMAN = "principal:human"
THIRD = "principal:third"
AGENT = "principal:agent"
FINANCE = "fragment:res:finance"
JOINT = "fragment:res:joint"
EVIDENCE = "fragment:evidence:bindings"

evaluator = AuthorityEvaluator()


def principal(principal_id: str, kind: PrincipalKind = PrincipalKind.HUMAN_OPERATOR) -> None:
    """Principal records exist for ref validation; the evaluator never
    consults kind (asserted by the operator-priority test)."""


def ref(record_id: str, revision: int = 1) -> RecordRef:
    return RecordRef(id=record_id, revision=revision)


def audience(destination_id: str = "room:public") -> AudienceSnapshot:
    return AudienceSnapshot(
        id=f"audience:{destination_id}",
        revision=1,
        created_at=NOW,
        destination_id=destination_id,
        environment_kind=EnvironmentKind.ROOM,
        visibility=Visibility.PUBLIC,
        known_principal_refs=(ref(HUMAN),),
        unknown_readers=True,
        history_visibility=HistoryVisibility.PUBLIC_HISTORY,
        completeness=Completeness.OBSERVED,
        disclosure_context_revision=1,
        valid_until=None,
    )


def resource(resource_id: str = FINANCE, commitments: tuple[str, ...] = ()) -> ContentFragment:
    return ContentFragment(
        id=resource_id,
        revision=1,
        created_at=NOW,
        kind=FragmentKind.TEXT,
        payload_ref=f"payload:{resource_id}",
        provenance=Provenance(
            producer_ref=ref(HUMAN),
            source_kind=SourceKind.SOCIAL_MESSAGE,
            origin_event_ref=None,
            claimed_speaker_ref=None,
            instruction_authority=InstructionAuthority.NONE,
            integrity=Integrity.EXTERNAL_UNVERIFIED,
            custodian_refs=(),
            subject_refs=(),
            original_audience_ref=None,
            assertion_status=AssertionStatus.ATTRIBUTED_ASSERTION,
            producer_coverage_ref=None,
            model_claimed_parent_refs=(),
            occurred_at=NOW,
            observed_at=NOW,
        ),
        commitment_refs=tuple(ref(cid) for cid in commitments),
        eligibility_status=EligibilityStatus.ACTIVE,
    )


def commitment(
    commitment_id: str,
    *,
    resource_id: str = FINANCE,
    internal_only: bool = True,
    observable: bool = False,
    expression: bool = False,
    strength: CommitmentStrength = CommitmentStrength.STRONG,
) -> Commitment:
    return Commitment(
        id=commitment_id,
        revision=1,
        created_at=NOW,
        subject_refs=(ref(HUMAN),),
        custodian_refs=(ref(HUMAN),),
        resource_id=resource_id,
        source_scope=None,
        strength=strength,
        internal_use_allowed=internal_only,
        observable_influence_allowed=observable,
        expression_allowed=expression,
        audience_predicate_id="pred:subject-only",
        policy_evidence_refs=(),
    )


def policy(
    policy_id: str = "policy:finance",
    *,
    resource_id: str = FINANCE,
    approval_rule: ApprovalRule = ApprovalRule.SINGLE_ISSUER,
    issuers: tuple[str, ...] = (HUMAN,),
    commitments: tuple[str, ...] = ("commit:finance",),
    transforms: tuple[str, ...] = ("FIXED_AFFORDABILITY_PREDICATE",),
    status: RecordStatus = RecordStatus.ACTIVE,
) -> ResourcePolicy:
    return ResourcePolicy(
        id=policy_id,
        revision=1,
        created_at=NOW,
        resource_id=resource_id,
        data_class="financial",
        approval_rule=approval_rule,
        eligible_issuer_refs=tuple(ref(i) for i in issuers),
        commitment_refs=tuple(ref(cid) for cid in commitments),
        allowed_transform_ids=transforms,
        policy_revision=1,
        status=status,
    )


def binding(
    binding_id: str,
    *,
    issuer: str = HUMAN,
    domain: AuthorityDomain = AuthorityDomain.RESOURCE_USE,
    resource_id: str = FINANCE,
    revoked_at: str | None = None,
    valid_until: str | None = None,
) -> AuthorityBinding:
    return AuthorityBinding(
        id=binding_id,
        revision=1,
        created_at=NOW,
        issuer_ref=ref(issuer),
        domain=domain,
        resource_id=resource_id,
        evidence_ref=ref(EVIDENCE),
        delegated_from=None,
        valid_until=valid_until,
        revoked_at=revoked_at,
    )


def authorization(
    auth_id: str = "use:finance-fields",
    *,
    resource_id: str = FINANCE,
    issuers: tuple[str, ...] = (HUMAN,),
    bindings: tuple[str, ...] = ("binding:human-finance",),
    operations: tuple[ResourceOperation, ...] = (ResourceOperation.READ,),
    purpose_classes: tuple[str, ...] = ("social-participation",),
    audience_bounds: tuple[str, ...] = ("room:public",),
    precision: RequestedPrecision = RequestedPrecision.FIELDS,
    transforms: tuple[str, ...] = ("FIXED_AFFORDABILITY_PREDICATE",),
    valid_until: str | None = None,
    status: RecordStatus = RecordStatus.ACTIVE,
) -> UseAuthorization:
    return UseAuthorization(
        id=auth_id,
        revision=1,
        created_at=NOW,
        resource_id=resource_id,
        issuer_refs=tuple(ref(i) for i in issuers),
        binding_refs=tuple(ref(b) for b in bindings),
        operations=operations,
        permitted_purpose_classes=purpose_classes,
        audience_class_bounds=audience_bounds,
        maximum_precision=precision,
        allowed_transform_ids=transforms,
        valid_until=valid_until,
        status=status,
    )


def intent(purpose_class: str = "social-participation") -> Intent:
    return Intent(
        id="intent:probe",
        revision=1,
        created_at=NOW,
        creator_ref=ref(AGENT),
        goal_fragment_ref=ref("fragment:goal"),
        purpose_class=purpose_class,
        observed_trigger_refs=(),
        endorsement_revision=1,
        producer_coverage_ref=None,
        runtime_task_root_ids=("root:1",),
        parent_work_ref=None,
        allowed_destination_refs=("room:public",),
        due_at=None,
        expires_at=None,
        state=IntentState.OPEN,
    )


def case(
    *,
    intended_use: IntendedUse = IntendedUse.INTERNAL_ONLY,
    precision: RequestedPrecision = RequestedPrecision.FIELDS,
    transform_id: str | None = None,
    resource_record: ContentFragment | None = None,
    policies: tuple[ResourcePolicy, ...] | None = None,
    bindings: tuple[AuthorityBinding, ...] | None = None,
    authorizations: tuple[UseAuthorization, ...] | None = None,
    commitments: tuple[Commitment, ...] = (),
    intent_record: Intent | None = None,
    audience_record: AudienceSnapshot | None = None,
) -> EvaluationInput:
    return EvaluationInput(
        intended_use=intended_use,
        requested_precision=precision,
        transform_id=transform_id,
        intent=intent_record if intent_record is not None else intent(),
        audience=audience_record if audience_record is not None else audience(),
        resource=resource_record if resource_record is not None else resource(),
        policies=policies if policies is not None else (policy(commitments=("commit:finance",)),),
        bindings=bindings if bindings is not None else (binding("binding:human-finance"),),
        authorizations=authorizations if authorizations is not None else (authorization(),),
        commitments=commitments
        if commitments
        else (commitment("commit:finance", internal_only=True),),
        now=NOW,
    )


# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------


def test_granted_partial_for_fields_precision() -> None:
    verdict = evaluator.evaluate(case())
    assert verdict.status == AccessStatus.GRANTED
    assert verdict.view_kind == ViewKind.PARTIAL
    assert verdict.reason_codes == ()
    assert verdict.permitted_uses == (IntendedUse.INTERNAL_ONLY,)
    assert verdict.authorizations[0].id == "use:finance-fields"
    assert verdict.inherited_commitments[0].id == "commit:finance"


def test_granted_full_and_abstracted_kinds() -> None:
    full = evaluator.evaluate(
        case(
            precision=RequestedPrecision.EXACT,
            authorizations=(authorization(precision=RequestedPrecision.EXACT),),
        )
    )
    assert (full.status, full.view_kind) == (AccessStatus.GRANTED, ViewKind.FULL)
    abstracted = evaluator.evaluate(
        case(
            precision=RequestedPrecision.ABSTRACT,
            authorizations=(authorization(precision=RequestedPrecision.ABSTRACT),),
        )
    )
    assert (abstracted.status, abstracted.view_kind) == (AccessStatus.GRANTED, ViewKind.ABSTRACTED)


def test_denied_when_authority_exists_but_use_not_authorized() -> None:
    verdict = evaluator.evaluate(case(authorizations=()))
    assert verdict.status == AccessStatus.DENIED
    assert ReasonCode.POLICY_FORBIDDEN in verdict.reason_codes
    assert verdict.view_kind == ViewKind.NONE
    assert verdict.permitted_uses == ()
    assert verdict.authorizations == ()


def test_pending_authority_when_no_active_binding() -> None:
    verdict = evaluator.evaluate(case(bindings=()))
    assert verdict.status == AccessStatus.PENDING_AUTHORITY
    assert ReasonCode.AUTHORITY_MISSING in verdict.reason_codes


def test_unavailable_when_resource_missing() -> None:
    from dataclasses import replace

    verdict = evaluator.evaluate(replace(case(), resource=None))
    assert verdict.status == AccessStatus.UNAVAILABLE


def test_conflict_from_duplicate_active_policies() -> None:
    verdict = evaluator.evaluate(
        case(policies=(policy(), policy("policy:finance-2", issuers=(THIRD,))))
    )
    assert verdict.status == AccessStatus.CONFLICT
    assert ReasonCode.AUTHORITY_CONFLICT in verdict.reason_codes


def test_conflict_from_contradictory_commitments_beats_pending() -> None:
    verdict = evaluator.evaluate(
        case(
            bindings=(),  # would be PENDING on its own
            commitments=(
                commitment("commit:a", internal_only=True),
                commitment("commit:b", internal_only=False),
            ),
            policies=(policy(commitments=("commit:a", "commit:b")),),
        )
    )
    assert verdict.status == AccessStatus.CONFLICT


# ---------------------------------------------------------------------------
# Quorum rules
# ---------------------------------------------------------------------------


def test_single_issuer_quorum_one_eligible_binding_suffices() -> None:
    verdict = evaluator.evaluate(
        case(
            policies=(policy(issuers=(HUMAN, THIRD)),),
            bindings=(binding("binding:human-finance"),),
        )
    )
    assert verdict.status == AccessStatus.GRANTED


def test_all_listed_requires_every_configured_issuer() -> None:
    joint_bindings = (
        binding("binding:human-joint", resource_id=JOINT),
        binding("binding:third-joint", issuer=THIRD, resource_id=JOINT),
    )
    joint_policy = policy(
        "policy:joint",
        resource_id=JOINT,
        approval_rule=ApprovalRule.ALL_LISTED,
        issuers=(HUMAN, THIRD),
        commitments=("commit:joint",),
    )
    joint_auth = authorization(
        "use:joint",
        resource_id=JOINT,
        issuers=(HUMAN, THIRD),
        bindings=("binding:human-joint", "binding:third-joint"),
        purpose_classes=("project-coordination",),
        precision=RequestedPrecision.ABSTRACT,
    )
    missing_one = evaluator.evaluate(
        case(
            policies=(joint_policy,),
            bindings=(joint_bindings[0],),  # third-party binding absent
            authorizations=(joint_auth,),
            commitments=(commitment("commit:joint", resource_id=JOINT),),
            resource_record=resource(JOINT),
        )
    )
    assert missing_one.status == AccessStatus.PENDING_AUTHORITY
    complete = evaluator.evaluate(
        case(
            precision=RequestedPrecision.ABSTRACT,
            policies=(joint_policy,),
            bindings=joint_bindings,
            authorizations=(joint_auth,),
            commitments=(commitment("commit:joint", resource_id=JOINT),),
            intent_record=intent("project-coordination"),
            resource_record=resource(JOINT),
        )
    )
    assert complete.status == AccessStatus.GRANTED


def test_non_eligible_issuer_binding_does_not_count() -> None:
    verdict = evaluator.evaluate(
        case(
            policies=(policy(issuers=(HUMAN,)),),
            bindings=(binding("binding:stranger", issuer=THIRD),),
        )
    )
    assert verdict.status == AccessStatus.PENDING_AUTHORITY


def test_revoked_or_expired_bindings_are_inactive() -> None:
    revoked = evaluator.evaluate(case(bindings=(binding("binding:human-finance", revoked_at=NOW),)))
    assert revoked.status == AccessStatus.PENDING_AUTHORITY
    expired = evaluator.evaluate(
        case(bindings=(binding("binding:human-finance", valid_until="2026-10-11T00:00:00Z"),))
    )
    assert expired.status == AccessStatus.PENDING_AUTHORITY


# ---------------------------------------------------------------------------
# Domain-scoped operations
# ---------------------------------------------------------------------------


def test_expression_requires_disclosure_domain_bindings() -> None:
    resource_use_only = evaluator.evaluate(
        case(
            intended_use=IntendedUse.EXPRESSION,
            authorizations=(
                authorization(operations=(ResourceOperation.READ, ResourceOperation.EXPRESS)),
            ),
        )
    )
    assert resource_use_only.status == AccessStatus.PENDING_AUTHORITY
    with_disclosure = evaluator.evaluate(
        case(
            intended_use=IntendedUse.EXPRESSION,
            bindings=(
                binding("binding:human-finance"),
                binding("binding:human-finance-disclosure", domain=AuthorityDomain.DISCLOSURE),
            ),
            authorizations=(
                authorization(
                    operations=(ResourceOperation.READ, ResourceOperation.EXPRESS),
                    bindings=("binding:human-finance", "binding:human-finance-disclosure"),
                ),
            ),
        )
    )
    assert with_disclosure.status == AccessStatus.GRANTED
    assert IntendedUse.EXPRESSION in with_disclosure.permitted_uses


def test_observable_influence_requires_disclosure_domain() -> None:
    verdict = evaluator.evaluate(
        case(
            intended_use=IntendedUse.OBSERVABLE_INFLUENCE,
            authorizations=(authorization(operations=(ResourceOperation.OBSERVABLE_INFLUENCE,)),),
        )
    )
    assert verdict.status == AccessStatus.PENDING_AUTHORITY


def test_transform_request_adds_transform_operation() -> None:
    verdict = evaluator.evaluate(
        case(
            transform_id="FIXED_AFFORDABILITY_PREDICATE",
            authorizations=(
                authorization(operations=(ResourceOperation.READ, ResourceOperation.TRANSFORM)),
            ),
        )
    )
    assert verdict.status == AccessStatus.GRANTED
    read_only = evaluator.evaluate(case(transform_id="FIXED_AFFORDABILITY_PREDICATE"))
    assert read_only.status == AccessStatus.DENIED


def test_unregistered_transform_id_denied() -> None:
    verdict = evaluator.evaluate(case(transform_id="DYNAMIC_SUMMARIZE"))
    assert verdict.status == AccessStatus.DENIED
    assert ReasonCode.UNSUPPORTED in verdict.reason_codes


# ---------------------------------------------------------------------------
# Dimension matching
# ---------------------------------------------------------------------------


def test_precision_above_maximum_denied() -> None:
    verdict = evaluator.evaluate(case(precision=RequestedPrecision.EXACT))
    assert verdict.status == AccessStatus.DENIED
    assert ReasonCode.VIEW_FORBIDDEN in verdict.reason_codes


def test_purpose_class_not_permitted_denied() -> None:
    verdict = evaluator.evaluate(case(intent_record=intent("curiosity")))
    assert verdict.status == AccessStatus.DENIED
    assert ReasonCode.POLICY_FORBIDDEN in verdict.reason_codes


def test_audience_out_of_bounds_denied() -> None:
    verdict = evaluator.evaluate(case(audience_record=audience("room:dm")))
    assert verdict.status == AccessStatus.DENIED
    assert ReasonCode.POLICY_FORBIDDEN in verdict.reason_codes


def test_expired_authorization_not_active() -> None:
    verdict = evaluator.evaluate(
        case(authorizations=(authorization(valid_until="2026-10-11T00:00:00Z"),))
    )
    assert verdict.status == AccessStatus.DENIED


def test_second_authorization_can_grant_where_first_fails() -> None:
    verdict = evaluator.evaluate(
        case(
            authorizations=(
                authorization("use:other-purpose", purpose_classes=("other",)),
                authorization(),
            )
        )
    )
    assert verdict.status == AccessStatus.GRANTED


def test_first_failing_dimension_reported_deterministically() -> None:
    verdict = evaluator.evaluate(
        case(
            precision=RequestedPrecision.EXACT,
            authorizations=(authorization(purpose_classes=("other",)),),
        )
    )
    assert verdict.status == AccessStatus.DENIED
    assert verdict.reason_codes == (ReasonCode.POLICY_FORBIDDEN,)


def test_no_policy_denies_registry_gated() -> None:
    verdict = evaluator.evaluate(case(policies=()))
    assert verdict.status == AccessStatus.DENIED
    assert ReasonCode.POLICY_FORBIDDEN in verdict.reason_codes


def test_unlinked_authorization_does_not_grant() -> None:
    verdict = evaluator.evaluate(
        case(
            bindings=(binding("binding:human-finance"),),
            authorizations=(authorization(bindings=("binding:someone-elses",)),),
        )
    )
    assert verdict.status == AccessStatus.DENIED


# ---------------------------------------------------------------------------
# Operator is not a universal key
# ---------------------------------------------------------------------------


def test_operator_without_binding_denies_like_any_principal() -> None:
    """Principal kind never enters the evaluation: an operator principal
    with no binding gets exactly the operator-less verdict."""
    principal("principal:operator", PrincipalKind.HUMAN_OPERATOR)
    without = evaluator.evaluate(case(bindings=()))
    # same shape when the (absent) issuer would be an operator: identical
    # verdict, because kind is not an input anywhere
    assert without.status == AccessStatus.PENDING_AUTHORITY
    # and a granted chain stays granted when the issuer is an operator:
    # authority comes from the binding, not from the kind
    operator_grant = evaluator.evaluate(
        case(
            bindings=(binding("binding:operator-finance", issuer="principal:operator"),),
            policies=(policy(issuers=("principal:operator",)),),
            authorizations=(
                authorization(
                    issuers=("principal:operator",), bindings=("binding:operator-finance",)
                ),
            ),
        )
    )
    assert operator_grant.status == AccessStatus.GRANTED


# ---------------------------------------------------------------------------
# Data-class combining
# ---------------------------------------------------------------------------


def test_data_class_request_combines_worst_case() -> None:
    granted = case()
    denied = case(
        policies=(policy("policy:other", resource_id="fragment:res:other"),),
        bindings=(binding("binding:other", resource_id="fragment:res:other"),),
        authorizations=(),
        resource_record=resource("fragment:res:other"),
    )
    assert evaluator.evaluate_class((granted, denied)).status == AccessStatus.DENIED
    assert evaluator.evaluate_class((granted,)).status == AccessStatus.GRANTED
    assert evaluator.evaluate_class(()).status == AccessStatus.DENIED


# ---------------------------------------------------------------------------
# Review(section 2) regressions
# ---------------------------------------------------------------------------


def test_all_listed_with_no_eligible_issuers_fails_closed() -> None:
    """An empty eligible-issuer set must never satisfy the quorum vacuously."""
    verdict = evaluator.evaluate(
        case(
            policies=(policy("policy:empty", approval_rule=ApprovalRule.ALL_LISTED, issuers=()),),
            bindings=(),
            authorizations=(authorization("use:empty", issuers=(), bindings=()),),
        )
    )
    assert verdict.status == AccessStatus.PENDING_AUTHORITY
    assert ReasonCode.AUTHORITY_MISSING in verdict.reason_codes
    assert verdict.permitted_uses == ()


def test_transform_output_precision_bounded_by_authorization() -> None:
    """An ABSTRACT-max authorization must not yield a FIELDS projection."""
    verdict = evaluator.evaluate(
        case(
            transform_id="FIXED_FIELD_VIEW",
            authorizations=(
                authorization(
                    precision=RequestedPrecision.ABSTRACT,
                    operations=(ResourceOperation.READ, ResourceOperation.TRANSFORM),
                    transforms=("FIXED_FIELD_VIEW",),
                ),
            ),
        )
    )
    assert verdict.status == AccessStatus.DENIED
    assert ReasonCode.VIEW_FORBIDDEN in verdict.reason_codes


def test_verdict_bindings_are_qualifying_only() -> None:
    """A cited-but-revoked binding never appears as the authorizing chain."""
    verdict = evaluator.evaluate(
        case(
            bindings=(
                binding("binding:human-finance"),
                binding("binding:human-finance-revoked", revoked_at=NOW),
            ),
            authorizations=(
                authorization(
                    bindings=("binding:human-finance", "binding:human-finance-revoked"),
                ),
            ),
        )
    )
    assert verdict.status == AccessStatus.GRANTED
    assert [b.id for b in verdict.bindings] == ["binding:human-finance"]


def test_class_grant_merges_every_members_restrictions() -> None:
    """A granted data-class verdict carries ALL members' commitments and
    authorizations, and the INTERSECTION of permitted uses."""
    member_a = case()  # finance: READ only -> INTERNAL_ONLY
    member_b = case(
        policies=(policy("policy:b", resource_id="fragment:res:b", commitments=("commit:b",)),),
        bindings=(binding("binding:b", resource_id="fragment:res:b"),),
        authorizations=(
            authorization(
                "use:b",
                resource_id="fragment:res:b",
                bindings=("binding:b",),
                operations=(ResourceOperation.READ, ResourceOperation.EXPRESS),
            ),
        ),
        commitments=(commitment("commit:b", resource_id="fragment:res:b"),),
        resource_record=resource("fragment:res:b"),
    )
    verdict = evaluator.evaluate_class((member_a, member_b))
    assert verdict.status == AccessStatus.GRANTED
    assert {c.id for c in verdict.inherited_commitments} == {"commit:finance", "commit:b"}
    assert {a.id for a in verdict.authorizations} == {"use:finance-fields", "use:b"}
    # member A only permits internal use: the class view permits only that
    assert verdict.permitted_uses == (IntendedUse.INTERNAL_ONLY,)


def test_class_duplicate_policies_on_one_member_conflict() -> None:
    member = case(
        policies=(policy(), policy("policy:dup", issuers=(THIRD,))),
    )
    verdict = evaluator.evaluate_class((member,))
    assert verdict.status == AccessStatus.CONFLICT
    assert ReasonCode.AUTHORITY_CONFLICT in verdict.reason_codes
