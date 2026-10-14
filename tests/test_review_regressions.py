"""Regressions for the GLM-flash review of M2 sections 4+5.

Every test here pins a finding from that review: the REVIEW_TIMEOUT and
non-fact-coverage HOLD paths, the §8.5 snapshot-change void, the ACT-only
ADVISORY rule (now documented), DEFER routing through the gate, the
necessity-text notice boundary, REQUEST_ACCESS malformed/denied turn paths,
the production selection→coverage→REQUIRED chain, the submissions budget,
propose idempotence, scope binding to the turn's destination, view-carried
commitment inheritance, policy-attached routing, and destination-audience
binding.
"""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from pathlib import Path

import pytest

from agora.commit_gate import CommitGate, GateContext
from agora.context_memory import MemoryStateError
from agora.contracts import (
    CapabilityEnvelope,
    Commitment,
    CommitmentStrength,
    DecisionKind,
    Disposition,
    EffectKind,
    FinishChoice,
    IntendedUse,
    MainStepKind,
    MemoryRecord,
    MemoryStage,
    MemoryView,
    ModelEffectDraft,
    ModelFinishStep,
    ModelRequestAccessStep,
    ModelSubmitPlanStep,
    OutcomeKind,
    ProducerCoverage,
    ProducerKind,
    RecordRef,
    RecordStatus,
    RequestedPrecision,
    ReviewEnvelope,
    ReviewRequirement,
    ScopeKind,
    SnapshotStatus,
)
from agora.ports import ReviewAssessmentData, ScriptedReviewer
from tests.fixtures_m2 import FINANCE_RESOURCE, JOINT_RESOURCE
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
)
from tests.test_review_routing import PASS_FACTS
from tests.test_story_affordability import access_step, finish_step, submit_step

DESCRIPTORS_PASS = ReviewAssessmentData(
    result="PASS", flags=(), runtime_assigned_coverage="DESCRIPTORS_ONLY"
)


def _record_count(stack: ReviewStack, record_type: str) -> int:
    row = stack.core._conn.execute(
        "SELECT count(*) AS n FROM core_records WHERE record_type = ?", (record_type,)
    ).fetchone()
    return int(row["n"])


def restricted_plan(stack: ReviewStack, tag: str, *, with_views: bool = True):
    """A plan whose runtime coverage carries the STRONG finance restriction,
    with or without a finance-matched comparison view."""
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
    plan = speak_plan(stack, f"plan:{tag}:1", snapshot=snapshot, intent_ref=intent)
    return plan, intent, snapshot


# ---------------------------------------------------------------------------
# REQUIRED protocol hold paths (review findings on the gate)
# ---------------------------------------------------------------------------


def test_required_review_without_a_reviewer_holds_with_timeout(tmp_path: Path) -> None:
    """A REQUIRED route with no reviewer port wired holds with
    REVIEW_TIMEOUT — nothing passes on a missing review (I07/§9)."""
    stack = ReviewStack(tmp_path)
    seed_five_classes(stack)
    plan, _intent, _snapshot = restricted_plan(stack, "t1")
    gate = CommitGate(stack.core, stack.clock, reviewer=None)

    decision = gate.evaluate(plan, context=gate_context(stack))

    assert decision.kind == DecisionKind.HOLD
    assert [code.value for code in decision.reason_codes] == ["REVIEW_TIMEOUT"]
    assert decision.authorization_ref is None


def test_descriptors_only_pass_does_not_authorize(tmp_path: Path) -> None:
    """A PASS whose claimed runtime coverage is DESCRIPTORS_ONLY holds with
    REVIEW_FACTS_MISSING even when comparison views were supplied."""
    stack = ReviewStack(tmp_path, assessments=[DESCRIPTORS_PASS])
    seed_five_classes(stack)
    plan, _intent, _snapshot = restricted_plan(stack, "t2", with_views=True)

    decision = stack.gate.evaluate(plan, context=gate_context(stack))

    assert decision.kind == DecisionKind.HOLD
    assert [code.value for code in decision.reason_codes] == ["REVIEW_FACTS_MISSING"]


def test_fact_claim_without_a_resource_matched_view_does_not_authorize(tmp_path: Path) -> None:
    """Runtime coverage is assigned by construction: a PASS claiming
    FACT_COMPARISON over an envelope whose only comparison view covers a
    DIFFERENT resource does not authorize the finance restriction."""
    stack = ReviewStack(tmp_path, assessments=[PASS_FACTS])
    seed_five_classes(stack)
    intent = make_intent(stack, "intent:t3")
    commit_ref = RecordRef(id="commit:finance-strong", revision=1)
    finance_ref = RecordRef(id=FINANCE_RESOURCE, revision=1)
    # the only comparison view is over the JOINT resource — finance has no
    # fact basis in the envelope
    foreign_view = comparison_view(
        stack,
        "view:t3",
        call_id="t3",
        fragment_ref=RecordRef(id=JOINT_RESOURCE, revision=1),
        authorization_id="use:joint-expression",
        inherited_commitment_refs=(),
    )
    snapshot = review_snapshot(
        stack,
        "t3",
        intent,
        selected_fragment_refs=(finance_ref,),
        inherited_commitment_refs=(commit_ref,),
        views=(foreign_view,),
    )
    plan = speak_plan(stack, "plan:t3:1", snapshot=snapshot, intent_ref=intent)

    decision = stack.gate.evaluate(plan, context=gate_context(stack))

    assert decision.kind == DecisionKind.HOLD
    assert [code.value for code in decision.reason_codes] == ["REVIEW_FACTS_MISSING"]


def test_snapshot_invalidation_after_assessment_voids_the_pass(tmp_path: Path) -> None:
    """§8.5 snapshot branch: a snapshot flip to INVALID after the assessment
    but before authorization voids the pass (POLICY_CHANGED)."""

    class SnapshotFlippingReviewer(ScriptedReviewer):
        def __init__(self, assessments, core) -> None:
            super().__init__(assessments)
            self._core = core

        def assess(self, envelope: ReviewEnvelope) -> ReviewAssessmentData:
            data = super().assess(envelope)
            from agora.contracts import ContextSnapshot

            snapshot = self._core.get_record(
                envelope.snapshot_ref.id, envelope.snapshot_ref.revision
            )
            assert isinstance(snapshot, ContextSnapshot)
            self._core.put_record(
                replace(snapshot, revision=snapshot.revision + 1, status=SnapshotStatus.INVALID)
            )
            return data

    stack = ReviewStack(tmp_path)
    seed_five_classes(stack)
    stack.gate.reviewer = SnapshotFlippingReviewer([PASS_FACTS], stack.core)
    plan, _intent, _snapshot = restricted_plan(stack, "t4", with_views=True)

    decision = stack.gate.evaluate(plan, context=gate_context(stack))

    assert decision.kind == DecisionKind.HOLD
    assert [code.value for code in decision.reason_codes] == ["POLICY_CHANGED"]
    assert decision.authorization_ref is None


def test_advisory_concerns_apply_to_act_plans_only(tmp_path: Path) -> None:
    """The documented rule: advice informs a draft, so ADVISORY routes ACT
    plans only — a QUIET resolution under configured concerns routes NONE
    with no reviewer call."""
    stack = ReviewStack(tmp_path, advisory_concerns=("concern:ordinary",))
    seed_five_classes(stack)
    intent = make_intent(stack, "intent:t5")
    snapshot = review_snapshot(stack, "t5", intent)
    plan = quiet_plan(stack, "plan:t5:1", snapshot=snapshot, intent_ref=intent)

    decision = stack.gate.evaluate(plan, context=gate_context(stack))

    assert decision.kind == DecisionKind.QUIET_COMPLETED
    assert stack.reviewer.calls == 0
    assert _record_count(stack, "ReviewRequirement") == 0


# ---------------------------------------------------------------------------
# DEFER routing (review finding: FINISH(DEFER) bypassed the gate)
# ---------------------------------------------------------------------------


def test_defer_under_strong_restriction_is_reviewed_and_held(tmp_path: Path) -> None:
    """A DEFER resolution under an active STRONG restriction routes REQUIRED
    first: with no comparison view the defer is HELD, and the intent is NOT
    deferred."""
    stack = ReviewStack(
        tmp_path,
        [ModelFinishStep(kind=MainStepKind.FINISH, outcome=FinishChoice.DEFER)],
        assessments=[PASS_FACTS],
    )
    seed_five_classes(stack)
    feed_event(stack)
    intent_ref = make_intent(stack, "intent:defer-r", goal_fragment_id=FINANCE_RESOURCE)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id, intent_ref=intent_ref)

    assert result.outcome.kind == OutcomeKind.HELD
    assert "REVIEW_FACTS_MISSING" in [code.value for code in result.outcome.reason_codes]
    assert stack.reviewer.calls == 1
    from agora.contracts import Intent, IntentState

    intent = stack.core.get_record(intent_ref.id)
    assert isinstance(intent, Intent)
    assert intent.state != IntentState.DEFERRED


def test_ordinary_defer_routes_none_and_defers_the_intent(tmp_path: Path) -> None:
    """An ordinary DEFER (no restriction) routes NONE and defers the intent
    with an explicit due time — the model's finish choice still works."""
    stack = ReviewStack(
        tmp_path,
        [ModelFinishStep(kind=MainStepKind.FINISH, outcome=FinishChoice.DEFER)],
    )
    seed_five_classes(stack)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id)

    assert result.outcome.kind == OutcomeKind.DEFERRED
    assert result.decision is not None
    assert result.decision.kind == DecisionKind.DEFERRED
    assert stack.reviewer.calls == 0
    from agora.contracts import Intent, IntentState

    assert result.outcome.intent_ref is not None
    intent = stack.core.get_record(result.outcome.intent_ref.id)
    assert isinstance(intent, Intent)
    assert intent.state == IntentState.DEFERRED
    assert intent.due_at is not None


# ---------------------------------------------------------------------------
# REQUEST_ACCESS turn paths and the notice boundary
# ---------------------------------------------------------------------------


def test_access_notice_carries_no_necessity_text(tmp_path: Path) -> None:
    """The typed notice reaches the next model call's envelope messages; the
    model-supplied necessity text (free text that could quote protected
    content) never does."""
    stack = ReviewStack(
        tmp_path,
        [
            access_step(
                resource_id=FINANCE_RESOURCE,
                transform_id="FIXED_AFFORDABILITY_PREDICATE",
                request_id="leak",
            ),
            submit_step("a careful public note"),
        ],
    )
    seed_five_classes(stack)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id)

    assert result.outcome.kind == OutcomeKind.COMMITTED
    rows = stack.core._conn.execute(
        "SELECT data FROM core_payloads WHERE payload_id LIKE 'payload:messages:%'"
    ).fetchall()
    assert rows
    notices = [row["data"] for row in rows if "access:" in row["data"]]
    assert notices, "the typed access notice must reach the next envelope"
    assert all("needed to answer the budget question safely" not in data for data in notices)


def test_data_class_request_grants_across_members(tmp_path: Path) -> None:
    """A data_class request (no resource id) takes the class path: the
    financial class grants a member view and the turn proceeds."""
    step = ModelRequestAccessStep(
        kind=MainStepKind.REQUEST_ACCESS,
        request={
            "request_id": "cls",
            "data_class": "financial",
            "intended_use": "INTERNAL_ONLY",
            "requested_precision": "FIELDS",
            "necessity_text": "needed to answer the budget question safely",
        },
    )
    stack = ReviewStack(tmp_path, [step, finish_step()])
    seed_five_classes(stack)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id)

    assert result.outcome.kind == OutcomeKind.NO_ACTION
    row = stack.core._conn.execute(
        "SELECT record_id FROM core_records WHERE record_type = 'MemoryAccessResult'"
    ).fetchone()
    from agora.contracts import MemoryAccessResult

    record = stack.core.get_record(row["record_id"])
    assert isinstance(record, MemoryAccessResult)
    assert record.access_status.value == "GRANTED"


def test_malformed_access_requests_are_typed_holds(tmp_path: Path) -> None:
    """Both resource_id and data_class (or neither) is INVALID_SCHEMA; an
    unresolvable resource is INVALID_REFERENCE — typed holds, never crashes."""

    def raw_request(request_id: str, **fields: object) -> ModelRequestAccessStep:
        request: dict = {
            "intended_use": "INTERNAL_ONLY",
            "requested_precision": "FIELDS",
        }
        request.update(fields)
        return ModelRequestAccessStep(
            kind=MainStepKind.REQUEST_ACCESS, request={"request_id": request_id, **request}
        )

    both = raw_request("both", resource_id=FINANCE_RESOURCE, data_class="financial")
    neither = raw_request("none")
    missing = raw_request("missing", resource_id="fragment:res:missing")

    for index, step in enumerate((both, neither, missing)):
        case_dir = tmp_path / f"case-{index}"
        case_dir.mkdir()
        stack = ReviewStack(case_dir, [step])
        seed_five_classes(stack)
        feed_event(stack)
        work_id = stack.coordinator.ready_work_ids()[0]
        result = stack.coordinator.run_turn(work_id)
        assert result.outcome.kind == OutcomeKind.HELD, step.request
        codes = [code.value for code in result.outcome.reason_codes]
        expected = "INVALID_REFERENCE" if step is missing else "INVALID_SCHEMA"
        assert expected in codes


def test_restricted_episode_fragment_routes_required_in_production(tmp_path: Path) -> None:
    """The production chain the hand-assembled fixtures bypass: an episode
    fragment carrying the finance commitment is selected into the snapshot,
    cited by the runtime coverage, and routes REQUIRED end to end."""
    stack = ReviewStack(tmp_path, [submit_step("about that budget")], assessments=[PASS_FACTS])
    seed_five_classes(stack)
    feed_event(stack, fragment_id=FINANCE_RESOURCE)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id)

    assert result.outcome.kind == OutcomeKind.HELD
    assert "REVIEW_FACTS_MISSING" in [code.value for code in result.outcome.reason_codes]
    assert stack.reviewer.calls == 1
    requirement_row = stack.core._conn.execute(
        "SELECT record_id FROM core_records WHERE record_type = 'ReviewRequirement'"
    ).fetchone()
    requirement = stack.core.get_record(requirement_row["record_id"])
    assert isinstance(requirement, ReviewRequirement)
    assert [ref.id for ref in requirement.relevant_commitment_refs] == ["commit:finance-strong"]


# ---------------------------------------------------------------------------
# Budgets, idempotence, scope binding, inheritance, routing inputs
# ---------------------------------------------------------------------------


def test_submissions_budget_bounds_the_rewrite_loop(tmp_path: Path) -> None:
    """The bounded rewrite loop: at most max_plan_submissions plans per turn;
    a fourth submission is refused and the standing outcome survives."""

    def denied_draft(text: str) -> ModelSubmitPlanStep:
        # a hard-denied plan (destination outside the capability) holds the
        # turn but leaves the revision opportunity open
        return ModelSubmitPlanStep(
            kind=MainStepKind.SUBMIT_PLAN,
            intent_ref=RecordRef(id="intent:turn:placeholder", revision=1),
            disposition=Disposition.ACT,
            effects=(
                ModelEffectDraft(
                    kind=EffectKind.SEND_MESSAGE,
                    destination_id="room:other",
                    text=text,
                    target_ref=None,
                    emoji=None,
                ),
            ),
            declared_fact_refs=(),
        )

    stack = ReviewStack(
        tmp_path,
        [
            denied_draft("draft one"),
            denied_draft("draft two"),
            denied_draft("draft three"),
            denied_draft("draft four"),
        ],
    )
    seed_five_classes(stack)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id)

    assert _record_count(stack, "EffectPlan") == 3
    assert result.outcome.kind == OutcomeKind.HELD  # the third decision stands
    assert "CAPABILITY_INVALID" in [code.value for code in result.outcome.reason_codes]


def test_identical_reproposals_are_idempotent(tmp_path: Path) -> None:
    """A crash-replayed PROPOSE_MEMORY re-proposes byte-identical content:
    the stored revision is the answer, never a duplicate."""
    stack = ReviewStack(tmp_path)
    seed_five_classes(stack)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    episode_fragment = RecordRef(id="fragment:evt:1", revision=1)
    snapshot = stack.context_memory.build_snapshot(
        identity_ref=RecordRef(id=stack.identity.id, revision=1),
        intent_ref=make_intent(stack, "intent:idem"),
        audience_ref=RecordRef(id=stack.audience.id, revision=1),
        episode_ref=RecordRef(id="episode:room:public", revision=1),
        sink_ref=stack.sink_ref,
        effect_capable=True,
        call_id="idem",
    )
    origin_kwargs = {
        "assertion_text": "the room prefers concise answers",
        "source_refs": (episode_fragment,),
        "requested_stage": MemoryStage.SOCIAL,
        "existing_memory_ref": None,
    }
    from agora.contracts import RuntimeOrigin

    origin = RuntimeOrigin(
        actor_ref=RecordRef(id="principal:agent", revision=1),
        work_ref=RecordRef(id=work_id, revision=1),
        snapshot_ref=RecordRef(id=snapshot.id, revision=snapshot.revision),
        trigger_event_refs=(),
        producer_coverage_ref=snapshot.producer_coverage_ref,
        runtime_epoch=stack.core.runtime_epoch,
    )
    first = stack.context_memory.propose_memory(
        **origin_kwargs,
        requested_scope_kind=ScopeKind.ROOM,
        scope_ref="room:public",
        runtime_origin=origin,
    )
    second = stack.context_memory.propose_memory(
        **origin_kwargs,
        requested_scope_kind=ScopeKind.ROOM,
        scope_ref="room:public",
        runtime_origin=origin,
    )
    assert first == second
    assert _record_count(stack, "MemoryRecord") == 1


def test_room_scope_is_bound_to_the_turns_destination(tmp_path: Path) -> None:
    """A DM-context observation cannot be stored as public-room-scoped: the
    ROOM scope must name the destination of the context that supplied the
    bytes; RELATIONSHIP scope has no curated mapping and is refused."""
    stack = ReviewStack(tmp_path)
    seed_five_classes(stack)
    feed_event(stack)
    from tests.test_adversarial_battery import dm_audience

    dm = dm_audience(stack)
    snapshot = stack.context_memory.build_snapshot(
        identity_ref=RecordRef(id=stack.identity.id, revision=1),
        intent_ref=make_intent(stack, "intent:scope"),
        audience_ref=dm,
        episode_ref=None,
        sink_ref=stack.sink_ref,
        effect_capable=True,
        call_id="scope:dm",
    )
    from agora.contracts import RuntimeOrigin

    origin = RuntimeOrigin(
        actor_ref=RecordRef(id="principal:agent", revision=1),
        work_ref=RecordRef(id="work:scope", revision=1),
        snapshot_ref=RecordRef(id=snapshot.id, revision=snapshot.revision),
        trigger_event_refs=(),
        producer_coverage_ref=snapshot.producer_coverage_ref,
        runtime_epoch=stack.core.runtime_epoch,
    )

    with pytest.raises(MemoryStateError) as public_refusal:
        stack.context_memory.propose_memory(
            assertion_text="seen in the DM",
            source_refs=(),
            requested_stage=MemoryStage.SOCIAL,
            requested_scope_kind=ScopeKind.ROOM,
            scope_ref="room:public",
            existing_memory_ref=None,
            runtime_origin=origin,
        )
    assert public_refusal.value.code.value == "POLICY_FORBIDDEN"

    with pytest.raises(MemoryStateError) as relationship_refusal:
        stack.context_memory.propose_memory(
            assertion_text="a bond",
            source_refs=(),
            requested_stage=MemoryStage.SOCIAL,
            requested_scope_kind=ScopeKind.RELATIONSHIP,
            scope_ref="principal:human",
            existing_memory_ref=None,
            runtime_origin=origin,
        )
    assert relationship_refusal.value.code.value == "POLICY_FORBIDDEN"

    same_room = stack.context_memory.propose_memory(
        assertion_text="seen in the DM",
        source_refs=(),
        requested_stage=MemoryStage.SOCIAL,
        requested_scope_kind=ScopeKind.ROOM,
        scope_ref="room:dm",
        existing_memory_ref=None,
        runtime_origin=origin,
    )
    assert same_room.revision == 1


def test_storage_inherits_view_carried_commitments(tmp_path: Path) -> None:
    """A fragment obtained through a view carries that view's inherited
    commitments into storage: an extra STRONG no-internal-use commitment
    refuses the store; an internal-use one is inherited, not laundered."""
    stack = ReviewStack(tmp_path)
    seed_five_classes(stack)
    feed_event(stack)
    now = stack.clock.now_rfc3339()
    extra_forbidden = Commitment(
        id="commit:extra-strict",
        revision=1,
        created_at=now,
        subject_refs=(RecordRef(id="principal:human", revision=1),),
        custodian_refs=(RecordRef(id="principal:human", revision=1),),
        resource_id=None,
        source_scope="room:dm",
        strength=CommitmentStrength.STRONG,
        internal_use_allowed=False,
        observable_influence_allowed=False,
        expression_allowed=False,
        audience_predicate_id="pred:subject-only",
        policy_evidence_refs=(),
    )
    stack.core.put_record(extra_forbidden)
    fragment_ref = RecordRef(id="fragment:evt:1", revision=1)
    intent = make_intent(stack, "intent:inh")

    def store_with_view(commitment_id: str, tag: str):
        coverage = ProducerCoverage(
            id=f"coverage:context:{tag}",
            revision=1,
            created_at=now,
            producer_kind=ProducerKind.CONTEXT_ASSEMBLY,
            input_fragment_refs=(fragment_ref,),
            input_view_refs=(RecordRef(id=f"view:{tag}", revision=1),),
            input_intent_refs=(intent,),
            input_policy_refs=(),
            sink_ref=stack.sink_ref,
            call_id=tag,
            runtime_epoch=stack.core.runtime_epoch,
        )
        view = MemoryView(
            id=f"view:{tag}",
            revision=1,
            created_at=now,
            resource_id=fragment_ref.id,
            returned_fragment_refs=(fragment_ref,),
            authorization_refs=(RecordRef(id="use:finance-fields", revision=1),),
            transform_certificate_ref=None,
            producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
            precision=RequestedPrecision.FIELDS,
            inherited_commitment_refs=(RecordRef(id=commitment_id, revision=1),),
            permitted_uses=(IntendedUse.EXPRESSION,),
            audience_bounds=("room:public",),
            valid_until=None,
            status=RecordStatus.ACTIVE,
        )
        stack.core.put_records((coverage, view))
        from agora.contracts import RuntimeOrigin

        origin = RuntimeOrigin(
            actor_ref=RecordRef(id="principal:agent", revision=1),
            work_ref=RecordRef(id=f"work:{tag}", revision=1),
            snapshot_ref=None,
            trigger_event_refs=(),
            producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
            runtime_epoch=stack.core.runtime_epoch,
        )
        return stack.context_memory.propose_memory(
            assertion_text=f"note {tag}",
            source_refs=(fragment_ref,),
            requested_stage=MemoryStage.SOCIAL,
            requested_scope_kind=ScopeKind.AGENT,
            scope_ref="agent",
            existing_memory_ref=None,
            runtime_origin=origin,
        )

    with pytest.raises(MemoryStateError) as refusal:
        store_with_view("commit:extra-strict", "inh-bad")
    assert refusal.value.code.value == "POLICY_FORBIDDEN"

    stored = store_with_view("commit:finance-strong", "inh-ok")
    memory = stack.core.get_record(stored.id, stored.revision)
    assert isinstance(memory, MemoryRecord)
    assert [ref.id for ref in memory.commitment_refs] == ["commit:finance-strong"]


def test_policy_attached_commitment_routes_required(tmp_path: Path) -> None:
    """A restriction arriving through the coverage's policies — not the
    fragments — still routes REQUIRED: the routing scan reads policy
    commitments as plan inputs."""
    stack = ReviewStack(tmp_path)
    seed_five_classes(stack)
    intent = make_intent(stack, "intent:pol")
    plain_fragment = RecordRef(id="fragment:res:project-fact", revision=1)
    snapshot = review_snapshot(
        stack,
        "pol",
        intent,
        selected_fragment_refs=(plain_fragment,),
        policy_refs=(RecordRef(id="policy:finance", revision=1),),
    )
    plan = speak_plan(stack, "plan:pol:1", snapshot=snapshot, intent_ref=intent)

    stack.gate.evaluate(plan, context=gate_context(stack))

    requirement_row = stack.core._conn.execute(
        "SELECT record_id FROM core_records WHERE record_type = 'ReviewRequirement'"
    ).fetchone()
    assert requirement_row is not None, "policy-attached STRONG commitment must route REQUIRED"
    requirement = stack.core.get_record(requirement_row["record_id"])
    assert isinstance(requirement, ReviewRequirement)
    assert "commit:finance-strong" in [ref.id for ref in requirement.relevant_commitment_refs]


def test_effect_destination_must_match_the_reviewed_audience(tmp_path: Path) -> None:
    """A review of room A's context cannot authorize delivery to room B,
    even when the capability allows both destinations."""
    stack = ReviewStack(tmp_path)
    seed_five_classes(stack)
    intent = make_intent(stack, "intent:dest")
    snapshot = review_snapshot(stack, "dest", intent)
    plan = speak_plan(stack, "plan:dest:1", snapshot=snapshot, intent_ref=intent)
    from agora.contracts import Effect

    effect = stack.core.get_record(plan.effects[0].id, plan.effects[0].revision)
    assert isinstance(effect, Effect)
    widened = CapabilityEnvelope(
        id=stack.capability.id,
        revision=stack.capability.revision + 1,
        created_at=stack.clock.now_rfc3339(),
        actor_ref=stack.capability.actor_ref,
        account_id=stack.capability.account_id,
        allowed_destination_ids=("room:public", "room:other"),
        allowed_effect_kinds=stack.capability.allowed_effect_kinds,
        provisioning_binding_refs=(),
        rate_budget_limits=(),
        valid_until=None,
        status=RecordStatus.ACTIVE,
    )
    stack.core.put_record(widened)

    # a fresh effect + plan genuinely addressed to room:other, bytes intact
    from agora.contracts import canonical_digest, canonical_json_bytes, render_send_message

    rendered = render_send_message(
        destination_id="room:other", text="elsewhere", reply_target_ref=None
    )
    stack.core.put_payload(
        "payload:effect:dest-other",
        canonical_json_bytes(rendered).decode("utf-8"),
        owner_ref="plan:dest:2",
    )
    redirect = dataclasses.replace(
        effect,
        id="plan:dest:2:effect:0",
        logical_effect_id="plan:dest:2:effect:0",
        destination_id="room:other",
        rendered_payload_ref="payload:effect:dest-other",
        payload_digest=canonical_digest(rendered),
    )
    stack.core.put_record(redirect)
    redirected_plan = dataclasses.replace(
        plan,
        id="plan:dest:2",
        effects=(redirect,),
        proposal_revision=2,
        proposal_digest=canonical_digest(
            {
                "disposition": plan.disposition.value,
                "effects": [redirect.payload_digest],
                "intent": plan.intent_ref.to_payload(),
                "snapshot": plan.snapshot_ref.id,
                "coverage": plan.producer_coverage_ref.id,
            }
        ),
    )
    stack.core.put_record(redirected_plan)
    context = GateContext(
        identity_ref=RecordRef(id=stack.identity.id, revision=stack.identity.revision),
        capability=widened,
        audience=stack.audience,
        max_message_chars=2000,
    )

    decision = stack.gate.evaluate(redirected_plan, context=context)

    assert decision.kind == DecisionKind.DENIED
    assert "CONTEXT_MISMATCH" in [code.value for code in decision.reason_codes]
