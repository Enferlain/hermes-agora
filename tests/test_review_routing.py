"""Review routing and the REQUIRED assessment protocol (M2 tasks 4.1/4.2).

Mirrors the spec scenarios in specs/replay/discretion/spec.md: ordinary
speech routes NONE with no reviewer call; an active STRONG expression
restriction routes REQUIRED and dispatch waits on a passing, version-
matched assessment; advisory concerns never block delivery; a fact-
dependent restriction without an authorized comparison view is HELD, not
evaluated on descriptors; a relevant policy revision landing after the
assessment voids the pass; and a resubmission after HOLD is a fresh route
at proposal_revision+1 whose failed assessment is never reused.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agora.contracts import (
    CommitAuthorization,
    Commitment,
    DecisionKind,
    DeliveryReceipt,
    EffectPlan,
    OutcomeKind,
    Pair,
    PerceptionSignals,
    PerceptionStatus,
    RecordRef,
    ResourcePolicy,
    ReviewAssessment,
    ReviewEnvelope,
    ReviewRequirement,
)
from agora.ports import ReviewAssessmentData, ScriptedReviewer
from tests.fixtures_m2 import FINANCE_RESOURCE
from tests.fixtures_review import (
    ReviewStack,
    comparison_view,
    feed_event,
    gate_context,
    make_intent,
    quiet_plan,
    review_snapshot,
    seed_five_classes,
    speak_plan,
    speak_step,
)

PASS_FACTS = ReviewAssessmentData(
    result="PASS", flags=(), runtime_assigned_coverage="FACT_COMPARISON"
)
CONCERNS = ReviewAssessmentData(
    result="CONCERNS", flags=(), runtime_assigned_coverage="DESCRIPTORS_ONLY"
)
INSUFFICIENT = ReviewAssessmentData(
    result="INSUFFICIENT_CONTEXT", flags=(), runtime_assigned_coverage="FACT_COMPARISON"
)


def restricted_plan(stack: ReviewStack, tag: str, *, with_views: bool) -> EffectPlan:
    """A plan whose context carries the STRONG finance restriction, with or
    without an authorized comparison view for the reviewer."""
    intent = make_intent(stack, f"intent:{tag}")
    commit_ref = RecordRef(id="commit:finance-strong", revision=1)
    finance_ref = RecordRef(id=FINANCE_RESOURCE, revision=1)
    views: tuple = ()
    if with_views:
        views = (
            comparison_view(
                stack,
                f"view:{tag}",
                call_id=tag,
                fragment_ref=finance_ref,
                authorization_id="use:finance-fields",
                inherited_commitment_refs=(commit_ref,),
            ),
        )
    snapshot = review_snapshot(
        stack,
        tag,
        intent,
        selected_fragment_refs=(finance_ref,),
        inherited_commitment_refs=(commit_ref,),
        views=views,
    )
    return speak_plan(stack, f"plan:{tag}:1", snapshot=snapshot, intent_ref=intent)


def _reasons(decision) -> list[str]:
    return [code.value for code in decision.reason_codes]


def _record_count(stack: ReviewStack, record_type: str) -> int:
    row = stack.core._conn.execute(
        "SELECT count(*) AS n FROM core_records WHERE record_type = ?", (record_type,)
    ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# Task 4.1: the §8 routing table
# ---------------------------------------------------------------------------


def test_ordinary_plan_routes_none_and_never_calls_the_reviewer(tmp_path: Path) -> None:
    """Spec scenario 'Ordinary speech needs no inference': no STRONG
    commitments anywhere in the plan's facts → route NONE, no reviewer."""
    stack = ReviewStack(tmp_path)
    seed_five_classes(stack)
    intent = make_intent(stack, "intent:t1")
    snapshot = review_snapshot(
        stack,
        "t1",
        intent,
        selected_fragment_refs=(RecordRef(id="fragment:res:project-fact", revision=1),),
    )
    plan = speak_plan(stack, "plan:t1:1", snapshot=snapshot, intent_ref=intent)

    decision = stack.gate.evaluate(plan, context=gate_context(stack))

    assert decision.kind == DecisionKind.AUTHORIZED
    assert decision.assessment_ref is None
    assert stack.reviewer.calls == 0
    assert stack.core.get_record("review-req:plan:t1:1:r1") is None
    assert _record_count(stack, "ReviewRequirement") == 0
    assert decision.authorization_ref is not None
    authorization = stack.core.get_record(
        decision.authorization_ref.id, decision.authorization_ref.revision
    )
    assert isinstance(authorization, CommitAuthorization)
    assert authorization.proposal_digest == plan.proposal_digest


def test_sensor_scores_alone_never_create_required(tmp_path: Path) -> None:
    """Sensor scores are not a routing input: a high leak probability
    sitting in the store cannot turn an ordinary plan into REQUIRED."""
    stack = ReviewStack(tmp_path)
    seed_five_classes(stack)
    stack.core.put_record(
        PerceptionSignals(
            id="perception:t9",
            revision=1,
            created_at=stack.clock.now_rfc3339(),
            event_refs=(),
            window_refs=(),
            probabilities=(Pair(first="leak", second=0.99),),
            address_flag=True,
            reply_flag=True,
            status=PerceptionStatus.KNOWN,
            adapter_version="perception:v1",
        )
    )
    intent = make_intent(stack, "intent:t9")
    snapshot = review_snapshot(stack, "t9", intent)
    plan = speak_plan(stack, "plan:t9:1", snapshot=snapshot, intent_ref=intent)

    decision = stack.gate.evaluate(plan, context=gate_context(stack))

    assert decision.kind == DecisionKind.AUTHORIZED
    assert stack.reviewer.calls == 0
    assert _record_count(stack, "ReviewRequirement") == 0


def test_strong_restriction_routes_required_and_waits_on_version_matched_pass(
    tmp_path: Path,
) -> None:
    """Spec scenario 'Strong restriction forces review': REQUIRED, and the
    dispatch waits on a passing version-matched assessment — the decision
    authorizes only after the scripted PASS with full fact coverage."""
    stack = ReviewStack(tmp_path, assessments=[PASS_FACTS])
    seed_five_classes(stack)
    plan = restricted_plan(stack, "t2", with_views=True)

    decision = stack.gate.evaluate(plan, context=gate_context(stack))

    assert decision.kind == DecisionKind.AUTHORIZED
    assert stack.reviewer.calls == 1

    requirement = stack.core.get_record("review-req:plan:t2:1:r1")
    assert isinstance(requirement, ReviewRequirement)
    assert requirement.route.value == "REQUIRED"
    assert [ref.id for ref in requirement.relevant_commitment_refs] == ["commit:finance-strong"]
    assert [coverage.value for coverage in requirement.expected_coverage] == ["FACT_COMPARISON"]
    assert {ref.id for ref in requirement.policy_version_vector} == {
        "commit:finance-strong",
        "policy:finance",
    }

    envelope = stack.core.get_record("review:plan:t2:1:r1")
    assert isinstance(envelope, ReviewEnvelope)
    assert envelope.proposal_digest == plan.proposal_digest
    assert [ref.id for ref in envelope.relevant_commitment_refs] == ["commit:finance-strong"]
    assert [ref.id for ref in envelope.relevant_policy_refs] == ["policy:finance"]
    assert [ref.id for ref in envelope.comparison_view_refs] == ["view:t2"]
    assert [ref.id for ref in envelope.expected_coverage_refs] == ["coverage:context:t2"]
    assert stack.core.get_payload(envelope.exact_messages_payload_ref) is not None

    assessment = stack.core.get_record("assessment:plan:t2:1:r1")
    assert isinstance(assessment, ReviewAssessment)
    assert assessment.result.value == "PASS"
    assert assessment.proposal_digest == plan.proposal_digest
    assert assessment.snapshot_revision == 1
    assert assessment.runtime_assigned_coverage.value == "FACT_COMPARISON"

    assert decision.authorization_ref is not None
    authorization = stack.core.get_record(
        decision.authorization_ref.id, decision.authorization_ref.revision
    )
    assert isinstance(authorization, CommitAuthorization)
    assert authorization.status.value == "ACTIVE"
    assert authorization.proposal_digest == plan.proposal_digest

    commitment = stack.core.get_record("commit:finance-strong")
    assert isinstance(commitment, Commitment)
    assert commitment.expression_allowed is False


# ---------------------------------------------------------------------------
# Task 4.2: the REQUIRED protocol
# ---------------------------------------------------------------------------


def test_facts_missing_holds_even_when_the_reviewer_passes(tmp_path: Path) -> None:
    """Spec scenario 'Facts missing holds the proposal': no authorized
    comparison view → HELD with REVIEW_FACTS_MISSING, never evaluated on
    descriptors alone — even a scripted PASS cannot wave it through."""
    stack = ReviewStack(tmp_path, assessments=[PASS_FACTS])
    seed_five_classes(stack)
    plan = restricted_plan(stack, "t4", with_views=False)

    decision = stack.gate.evaluate(plan, context=gate_context(stack))

    assert decision.kind == DecisionKind.HOLD
    assert _reasons(decision) == ["REVIEW_FACTS_MISSING"]
    assert decision.authorization_ref is None
    assert stack.core.get_record("auth:plan:t4:1:effect:0:1") is None
    # the passing assessment is recorded, but it does not authorize
    assessment = stack.core.get_record("assessment:plan:t4:1:r1")
    assert isinstance(assessment, ReviewAssessment)
    assert assessment.result.value == "PASS"
    assert decision.assessment_ref is not None
    assert stack.reviewer.calls == 1


def test_insufficient_context_holds_the_proposal(tmp_path: Path) -> None:
    """INSUFFICIENT_CONTEXT from the reviewer holds with the facts-missing
    code even when comparison views were authorized."""
    stack = ReviewStack(tmp_path, assessments=[INSUFFICIENT])
    seed_five_classes(stack)
    plan = restricted_plan(stack, "t10", with_views=True)

    decision = stack.gate.evaluate(plan, context=gate_context(stack))

    assert decision.kind == DecisionKind.HOLD
    assert _reasons(decision) == ["REVIEW_FACTS_MISSING"]
    assert decision.authorization_ref is None
    assert stack.reviewer.calls == 1


def test_policy_revision_during_review_voids_the_pass(tmp_path: Path) -> None:
    """Spec scenario 'Version change voids a pass': a relevant policy
    revision landing after the assessment but before dispatch means the
    assessment no longer authorizes the current bytes (§8.5)."""

    class RevisionLandingReviewer(ScriptedReviewer):
        """Lands policy:finance revision 2 while the reviewer waits."""

        def __init__(self, assessments, core) -> None:
            super().__init__(assessments)
            self._core = core

        def assess(self, envelope):
            data = super().assess(envelope)
            policy = self._core.get_record("policy:finance")
            assert isinstance(policy, ResourcePolicy)
            self._core.put_record(replace(policy, revision=2))
            return data

    stack = ReviewStack(tmp_path)
    seed_five_classes(stack)
    stack.gate.reviewer = RevisionLandingReviewer([PASS_FACTS], stack.core)
    plan = restricted_plan(stack, "t5", with_views=True)

    decision = stack.gate.evaluate(plan, context=gate_context(stack))

    assert decision.kind == DecisionKind.HOLD
    assert _reasons(decision) == ["POLICY_CHANGED"]
    assert decision.authorization_ref is None
    assert stack.core.get_record("auth:plan:t5:1:effect:0:1") is None
    # the pass was real, and the revision really landed — but after it, so
    # the bound versions no longer match
    assessment = stack.core.get_record("assessment:plan:t5:1:r1")
    assert isinstance(assessment, ReviewAssessment)
    assert assessment.result.value == "PASS"
    assert stack.core.get_record("policy:finance", 2) is not None
    reviewer = stack.gate.reviewer
    assert isinstance(reviewer, ScriptedReviewer)
    assert reviewer.calls == 1


def test_resubmission_after_hold_routes_fresh_at_next_revision(tmp_path: Path) -> None:
    """No downgrade on resubmission: the rewrite is proposal_revision+1,
    fully re-routed, with its own assessment record — the failed assessment
    is never reused to authorize."""
    stack = ReviewStack(tmp_path, assessments=[CONCERNS, PASS_FACTS])
    seed_five_classes(stack)

    intent = make_intent(stack, "intent:t6")
    commit_ref = RecordRef(id="commit:finance-strong", revision=1)
    finance_ref = RecordRef(id=FINANCE_RESOURCE, revision=1)
    view = comparison_view(
        stack,
        "view:t6",
        call_id="t6",
        fragment_ref=finance_ref,
        authorization_id="use:finance-fields",
        inherited_commitment_refs=(commit_ref,),
    )
    snapshot = review_snapshot(
        stack,
        "t6",
        intent,
        selected_fragment_refs=(finance_ref,),
        inherited_commitment_refs=(commit_ref,),
        views=(view,),
    )
    plan1 = speak_plan(
        stack, "plan:t6:1", snapshot=snapshot, intent_ref=intent, proposal_revision=1
    )
    plan2 = speak_plan(
        stack, "plan:t6:2", snapshot=snapshot, intent_ref=intent, proposal_revision=2
    )

    first = stack.gate.evaluate(plan1, context=gate_context(stack))
    second = stack.gate.evaluate(plan2, context=gate_context(stack))

    assert first.kind == DecisionKind.HOLD
    assert _reasons(first) == ["REVIEW_CONCERN"]
    assert first.assessment_ref is not None
    assert first.assessment_ref.id == "assessment:plan:t6:1:r1"
    assert second.kind == DecisionKind.AUTHORIZED
    assert second.assessment_ref is not None
    assert second.assessment_ref.id == "assessment:plan:t6:2:r2"
    # both revisions were routed from scratch; the failed assessment stays
    # a distinct record that the new revision never cites
    assert _record_count(stack, "ReviewRequirement") == 2
    assert stack.core.get_record("review-req:plan:t6:1:r1") is not None
    assert stack.core.get_record("review-req:plan:t6:2:r2") is not None
    assert stack.reviewer.calls == 2
    assert first.assessment_ref != second.assessment_ref


def test_quiet_under_strong_restriction_is_reviewed_and_hold_stays_local(
    tmp_path: Path,
) -> None:
    """A QUIET resolution under an active STRONG restriction still routes
    REQUIRED: a concerned reviewer holds it (staying local, no dispatch,
    no automatic fallback), while a passing resolution completes quietly."""
    stack = ReviewStack(tmp_path, assessments=[CONCERNS, PASS_FACTS])
    seed_five_classes(stack)
    intent = make_intent(stack, "intent:t7")
    commit_ref = RecordRef(id="commit:finance-strong", revision=1)
    finance_ref = RecordRef(id=FINANCE_RESOURCE, revision=1)
    view = comparison_view(
        stack,
        "view:t7",
        call_id="t7",
        fragment_ref=finance_ref,
        authorization_id="use:finance-fields",
        inherited_commitment_refs=(commit_ref,),
    )
    snapshot = review_snapshot(
        stack,
        "t7",
        intent,
        selected_fragment_refs=(finance_ref,),
        inherited_commitment_refs=(commit_ref,),
        views=(view,),
    )
    plan1 = quiet_plan(
        stack, "plan:t7:1", snapshot=snapshot, intent_ref=intent, proposal_revision=1
    )
    plan2 = quiet_plan(
        stack, "plan:t7:2", snapshot=snapshot, intent_ref=intent, proposal_revision=2
    )

    first = stack.gate.evaluate(plan1, context=gate_context(stack))
    second = stack.gate.evaluate(plan2, context=gate_context(stack))

    assert first.kind == DecisionKind.HOLD
    assert _reasons(first) == ["REVIEW_CONCERN"]
    assert second.kind == DecisionKind.QUIET_COMPLETED
    assert second.assessment_ref is not None
    assert second.assessment_ref.id == "assessment:plan:t7:2:r2"
    # silence stays local in both cases: no authorization was ever created
    assert _record_count(stack, "CommitAuthorization") == 0
    assert stack.reviewer.calls == 2


# ---------------------------------------------------------------------------
# Whole-turn integration (coordinator path)
# ---------------------------------------------------------------------------


def test_advisory_concerns_do_not_block_delivery(tmp_path: Path) -> None:
    """Spec scenario 'Advisory can be declined': the recorded advice does
    not block delivery — the turn commits and the world sees the bytes."""
    stack = ReviewStack(
        tmp_path,
        [speak_step("morning all")],
        assessments=[CONCERNS],
        advisory_concerns=("concern:ordinary",),
    )
    seed_five_classes(stack)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id)

    assert result.outcome.kind == OutcomeKind.COMMITTED
    assert result.decision is not None
    assert result.decision.kind == DecisionKind.AUTHORIZED
    assert result.decision.assessment_ref is not None
    assessment = stack.core.get_record(
        result.decision.assessment_ref.id, result.decision.assessment_ref.revision
    )
    assert isinstance(assessment, ReviewAssessment)
    assert assessment.result.value == "CONCERNS"
    row = stack.core._conn.execute(
        "SELECT record_id FROM core_records WHERE record_type = 'ReviewRequirement'"
    ).fetchone()
    requirement = stack.core.get_record(row["record_id"])
    assert isinstance(requirement, ReviewRequirement)
    assert requirement.route.value == "ADVISORY"
    assert stack.reviewer.calls == 1
    assert result.receipt_ref is not None
    receipt = stack.core.get_record(result.receipt_ref.id, result.receipt_ref.revision)
    assert isinstance(receipt, DeliveryReceipt)
    assert receipt.state.value == "SENT"
    assert stack.transport.outbox_state(f"{work_id}:effect:0") == "SENT"


def test_coordinator_derived_intent_restriction_holds_the_turn(tmp_path: Path) -> None:
    """End-to-end: the STRONG restriction rides the plan's derived intent
    (its goal fragment is the restricted finance resource), routes
    REQUIRED, and the turn holds with REVIEW_FACTS_MISSING — no dispatch,
    no fallback. (Fragment selection into snapshots needs episode wiring
    from task 5.1; the derived-intent source is wired today.)"""
    stack = ReviewStack(tmp_path, [speak_step("so about the budget")], assessments=[PASS_FACTS])
    seed_five_classes(stack)
    feed_event(stack)
    intent_ref = make_intent(stack, "intent:restricted", goal_fragment_id=FINANCE_RESOURCE)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id, intent_ref=intent_ref)

    assert result.outcome.kind == OutcomeKind.HELD
    assert "REVIEW_FACTS_MISSING" in [code.value for code in result.outcome.reason_codes]
    assert result.decision is not None
    assert result.decision.kind == DecisionKind.HOLD
    assert result.decision.authorization_ref is None
    assert stack.reviewer.calls == 1
    assert stack.transport._conn.execute("SELECT count(*) FROM transport_outbox").fetchone()[0] == 0
    assert stack.world._conn.execute("SELECT count(*) FROM world_effects").fetchone()[0] == 0
