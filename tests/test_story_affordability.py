"""Handoff story 2 (M2 task 5.1): the public affordability probe.

A public affordability question wakes a turn; the model requests protected
access; the request either lacks a basis or yields a constrained view. The
main path then revises, jokes, or stays quiet — a joke with no restricted
bytes commits with no review, a quiet resolution under an active STRONG
restriction is held for review (facts missing without comparison views), an
authorized joint-expression view commits only through a passing REQUIRED
assessment, concerns suppress, and a rewrite after a hold is a fully fresh
routed revision. Later scheduled behavior on the same intent retains
producer coverage, and PROPOSE_MEMORY binds runtime origin with verified
lineage: forged citations and scope widening are typed holds.
"""

from __future__ import annotations

from pathlib import Path

from agora.contracts import (
    ContentFragment,
    ContextSnapshot,
    Disposition,
    EffectKind,
    EffectPlan,
    FinishChoice,
    IntendedUse,
    MainStepKind,
    MemoryRecord,
    MemoryStage,
    MemoryView,
    ModelEffectDraft,
    ModelFinishStep,
    ModelProposeMemoryStep,
    ModelRequestAccessStep,
    ModelSubmitPlanStep,
    OutcomeKind,
    ProducerCoverage,
    RecordRef,
    ScopeKind,
)
from tests.fixtures_m2 import FINANCE_RESOURCE, JOINT_RESOURCE
from tests.fixtures_review import ReviewStack, feed_event, make_intent, seed_five_classes
from tests.test_review_routing import CONCERNS, PASS_FACTS


def access_step(
    *,
    resource_id: str,
    intended_use: str = "INTERNAL_ONLY",
    precision: str = "FIELDS",
    transform_id: str | None = None,
    audience_ref: dict | None = None,
    request_id: str = "probe",
) -> ModelRequestAccessStep:
    request: dict = {
        "request_id": request_id,
        "resource_id": resource_id,
        "intended_use": intended_use,
        "requested_precision": precision,
        "necessity_text": "needed to answer the budget question safely",
    }
    if transform_id is not None:
        request["transform_id"] = transform_id
    if audience_ref is not None:
        request["audience_ref"] = audience_ref
    return ModelRequestAccessStep(kind=MainStepKind.REQUEST_ACCESS, request=request)


def submit_step(text: str) -> ModelSubmitPlanStep:
    # The step's intent_ref is a placeholder: the runtime binds the turn's
    # resolved intent, never the model's claim.
    return ModelSubmitPlanStep(
        kind=MainStepKind.SUBMIT_PLAN,
        intent_ref=RecordRef(id="intent:turn:placeholder", revision=1),
        disposition=Disposition.ACT,
        effects=(
            ModelEffectDraft(
                kind=EffectKind.SEND_MESSAGE,
                destination_id="room:public",
                text=text,
                target_ref=None,
                emoji=None,
            ),
        ),
        declared_fact_refs=(),
    )


def quiet_step() -> ModelSubmitPlanStep:
    return ModelSubmitPlanStep(
        kind=MainStepKind.SUBMIT_PLAN,
        intent_ref=RecordRef(id="intent:turn:placeholder", revision=1),
        disposition=Disposition.QUIET,
        effects=(),
        declared_fact_refs=(),
    )


def finish_step() -> ModelFinishStep:
    return ModelFinishStep(kind=MainStepKind.FINISH, outcome=FinishChoice.NO_ACTION)


def propose_step(
    text: str,
    source_ids: tuple[str, ...],
    *,
    stage: MemoryStage = MemoryStage.SOCIAL,
    scope_kind: ScopeKind = ScopeKind.ROOM,
    scope_ref: str = "room:public",
) -> ModelProposeMemoryStep:
    return ModelProposeMemoryStep(
        kind=MainStepKind.PROPOSE_MEMORY,
        assertion_text=text,
        source_refs=tuple(RecordRef(id=source_id, revision=1) for source_id in source_ids),
        requested_stage=stage,
        requested_scope_kind=scope_kind,
        scope_ref=scope_ref,
        existing_memory_ref=None,
    )


def _outcome_codes(result) -> list[str]:
    return [code.value for code in result.outcome.reason_codes]


def _record_count(stack: ReviewStack, record_type: str) -> int:
    row = stack.core._conn.execute(
        "SELECT count(*) AS n FROM core_records WHERE record_type = ?", (record_type,)
    ).fetchone()
    return int(row["n"])


def _snapshot_of(stack: ReviewStack, result) -> ContextSnapshot:
    assert result.decision is not None
    plan_row = stack.core._conn.execute(
        "SELECT record_id FROM core_records WHERE record_type = 'EffectPlan'"
        " ORDER BY created_at, record_id"
    ).fetchall()
    assert plan_row
    plan = stack.core.get_record(plan_row[-1]["record_id"])
    assert isinstance(plan, EffectPlan)
    snapshot = stack.core.get_record(plan.snapshot_ref.id, plan.snapshot_ref.revision)
    assert isinstance(snapshot, ContextSnapshot)
    return snapshot


# ---------------------------------------------------------------------------
# Constrained view: the affordability predicate stays internal
# ---------------------------------------------------------------------------


def test_affordability_probe_then_joke_commits_without_review(tmp_path: Path) -> None:
    """The probe yields an internal-only predicate view; the joke carries no
    restricted bytes, so the route is NONE and the message commits — the
    internal-only view never enters any snapshot of the turn."""
    stack = ReviewStack(
        tmp_path,
        [
            access_step(resource_id=FINANCE_RESOURCE, transform_id="FIXED_AFFORDABILITY_PREDICATE"),
            submit_step("my wallet has strong opinions about this"),
            finish_step(),
        ],
    )
    seed_five_classes(stack)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id)

    assert result.outcome.kind == OutcomeKind.COMMITTED
    assert result.receipt_ref is not None
    # the constrained view exists and is internal-only …
    view_row = stack.core._conn.execute(
        "SELECT record_id FROM core_records WHERE record_type = 'MemoryView'"
    ).fetchone()
    view = stack.core.get_record(view_row["record_id"])
    assert isinstance(view, MemoryView)
    assert view.permitted_uses == (IntendedUse.INTERNAL_ONLY,)
    # … but never entered the committed plan's snapshot
    snapshot = _snapshot_of(stack, result)
    assert snapshot.active_view_refs == ()
    # ordinary speech: no reviewer, no review records
    assert stack.reviewer.calls == 0
    assert _record_count(stack, "ReviewRequirement") == 0


def test_quiet_under_restricted_intent_holds_for_missing_facts(tmp_path: Path) -> None:
    """Staying quiet under an active STRONG finance restriction routes
    REQUIRED; with no authorized comparison view the quiet resolution is
    HELD with REVIEW_FACTS_MISSING — no dispatch, no fallback, and the hold
    stands when the model has nothing more to offer."""
    stack = ReviewStack(
        tmp_path,
        [
            access_step(resource_id=FINANCE_RESOURCE, transform_id="FIXED_AFFORDABILITY_PREDICATE"),
            quiet_step(),
        ],
        assessments=[PASS_FACTS],
    )
    seed_five_classes(stack)
    feed_event(stack)
    intent_ref = make_intent(stack, "intent:restricted", goal_fragment_id=FINANCE_RESOURCE)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id, intent_ref=intent_ref)

    assert result.outcome.kind == OutcomeKind.HELD
    assert "REVIEW_FACTS_MISSING" in _outcome_codes(result)
    assert result.decision is not None
    assert result.decision.authorization_ref is None
    assert stack.reviewer.calls == 1
    assert stack.transport._conn.execute("SELECT count(*) FROM transport_outbox").fetchone()[0] == 0
    assert stack.world._conn.execute("SELECT count(*) FROM world_effects").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Allowed influence: the joint expression view commits under review
# ---------------------------------------------------------------------------


def test_joint_expression_view_commits_under_passing_review(tmp_path: Path) -> None:
    """An authorized joint-expression view is offered to the next snapshot,
    inherits the joint STRONG commitment (REQUIRED), supplies the reviewer's
    comparison view, and commits only on a version-matched PASS."""
    stack = ReviewStack(
        tmp_path,
        [
            access_step(
                resource_id=JOINT_RESOURCE, intended_use="EXPRESSION", precision="ABSTRACT"
            ),
            submit_step("the joint roadmap note: we are on track"),
        ],
        assessments=[PASS_FACTS],
    )
    seed_five_classes(stack)
    feed_event(stack)
    coordination = make_intent(stack, "intent:coord", purpose_class="project-coordination")
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id, intent_ref=coordination)

    assert result.outcome.kind == OutcomeKind.COMMITTED
    assert result.decision is not None
    assert result.decision.assessment_ref is not None
    # the granted view was admitted to the effect-capable snapshot and its
    # STRONG commitment traveled with it
    snapshot = _snapshot_of(stack, result)
    assert snapshot.active_view_refs
    assert "commit:joint-strong" in [ref.id for ref in snapshot.inherited_commitment_refs]
    # the plan's runtime coverage cites the view as a producer input
    coverage = stack.core.get_record(snapshot.producer_coverage_ref.id)
    assert isinstance(coverage, ProducerCoverage)
    assert [ref.id for ref in coverage.input_view_refs] == [snapshot.active_view_refs[0].id]
    assert stack.reviewer.calls == 1
    assert result.receipt_ref is not None
    assert stack.transport.outbox_state(f"{work_id}:effect:0") == "SENT"


def test_joint_expression_concerns_suppress_delivery(tmp_path: Path) -> None:
    """A concerned required review holds the joint note: no authorization,
    no dispatch, and the hold stands when the script ends."""
    stack = ReviewStack(
        tmp_path,
        [
            access_step(
                resource_id=JOINT_RESOURCE, intended_use="EXPRESSION", precision="ABSTRACT"
            ),
            submit_step("the joint roadmap note: we are on track"),
        ],
        assessments=[CONCERNS],
    )
    seed_five_classes(stack)
    feed_event(stack)
    coordination = make_intent(stack, "intent:coord", purpose_class="project-coordination")
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id, intent_ref=coordination)

    assert result.outcome.kind == OutcomeKind.HELD
    assert "REVIEW_CONCERN" in _outcome_codes(result)
    assert result.decision is not None
    assert result.decision.authorization_ref is None
    assert _record_count(stack, "CommitAuthorization") == 0
    assert stack.transport._conn.execute("SELECT count(*) FROM transport_outbox").fetchone()[0] == 0


def test_revision_after_hold_is_a_fresh_routed_submission(tmp_path: Path) -> None:
    """Revise after a held note: the rewrite is proposal_revision+1, fully
    re-routed with its own requirement and assessment — the failed
    assessment is never reused to authorize (no downgrade on resubmission)."""
    stack = ReviewStack(
        tmp_path,
        [
            access_step(
                resource_id=JOINT_RESOURCE, intended_use="EXPRESSION", precision="ABSTRACT"
            ),
            submit_step("a draft that overstates the budget"),
            submit_step("a careful joint note"),
        ],
        assessments=[CONCERNS, PASS_FACTS],
    )
    seed_five_classes(stack)
    feed_event(stack)
    coordination = make_intent(stack, "intent:coord", purpose_class="project-coordination")
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id, intent_ref=coordination)

    assert result.outcome.kind == OutcomeKind.COMMITTED
    assert result.decision is not None
    assert result.decision.assessment_ref is not None
    assert result.decision.assessment_ref.id.endswith(":r2")
    assert _record_count(stack, "ReviewRequirement") == 2
    assert _record_count(stack, "ReviewAssessment") == 2
    assert stack.reviewer.calls == 2


def test_later_turn_on_same_intent_retains_producer_coverage(tmp_path: Path) -> None:
    """Later scheduled behavior (story 2's tail): a second wake on the same
    coordination intent re-requests the joint view and commits again; every
    granted view's coverage still cites the joint resource, so the runtime
    lineage across turns is preserved."""
    joint_access = (
        access_step(resource_id=JOINT_RESOURCE, intended_use="EXPRESSION", precision="ABSTRACT"),
    )
    stack = ReviewStack(
        tmp_path,
        [
            *joint_access,
            submit_step("first careful joint note"),
            *joint_access,
            submit_step("second careful joint note"),
        ],
        assessments=[PASS_FACTS, PASS_FACTS],
    )
    seed_five_classes(stack)
    feed_event(stack)
    coordination = make_intent(stack, "intent:coord", purpose_class="project-coordination")
    first_work = stack.coordinator.ready_work_ids()[0]
    first = stack.coordinator.run_turn(first_work, intent_ref=coordination)
    assert first.outcome.kind == OutcomeKind.COMMITTED

    feed_event(stack, seq=2)
    second_work = stack.coordinator.ready_work_ids()[0]
    second = stack.coordinator.run_turn(second_work, intent_ref=coordination)
    assert second.outcome.kind == OutcomeKind.COMMITTED

    # both turns' plans ran on an authorized view of the joint resource —
    # the producer-coverage lineage is retained across the later wake, and
    # each admitted view keeps its snapshot as a dependency child
    view_rows = stack.core._conn.execute(
        "SELECT record_id FROM core_records WHERE record_type = 'MemoryView'"
        " AND record_id LIKE 'view:work:turn%'"
    ).fetchall()
    assert len(view_rows) == 2
    admitted_snapshots = []
    for row in view_rows:
        view = stack.core.get_record(row["record_id"])
        assert isinstance(view, MemoryView)
        assert view.resource_id == JOINT_RESOURCE
        children = stack.core.children_of(view.id)
        admitted_snapshots.extend(child for child in children if child.startswith("snapshot:"))
    assert len(admitted_snapshots) == 2


# ---------------------------------------------------------------------------
# PROPOSE_MEMORY: runtime-origin binding and lineage
# ---------------------------------------------------------------------------


def test_propose_memory_stores_scoped_memory_with_verified_lineage(tmp_path: Path) -> None:
    """An ordinary observation is retained as a SOCIAL room-scoped memory
    citing the turn's runtime coverage; the stored fragment records the
    model-claimed parents the runtime actually supplied."""
    stack = ReviewStack(
        tmp_path,
        [
            propose_step("the room prefers concise answers", ("fragment:evt:1",)),
            finish_step(),
        ],
    )
    seed_five_classes(stack)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id)

    assert result.outcome.kind == OutcomeKind.NO_ACTION
    rows = stack.core._conn.execute(
        "SELECT record_id FROM core_records WHERE record_type = 'MemoryRecord'"
    ).fetchall()
    assert len(rows) == 1
    memory = stack.core.get_record(rows[0]["record_id"])
    assert isinstance(memory, MemoryRecord)
    assert memory.stage == MemoryStage.SOCIAL
    assert memory.scope_kind == ScopeKind.ROOM
    assert memory.scope_ref == "room:public"

    fragment = stack.core.get_record(
        memory.assertion_fragment_ref.id, memory.assertion_fragment_ref.revision
    )
    assert isinstance(fragment, ContentFragment)
    assert fragment.provenance.producer_coverage_ref is not None
    assert [ref.id for ref in fragment.provenance.model_claimed_parent_refs] == ["fragment:evt:1"]


def test_propose_memory_forged_lineage_is_a_typed_hold(tmp_path: Path) -> None:
    """Citing a restricted fragment that was never supplied by the runtime
    context is a typed INVALID_REFERENCE hold — no memory is written."""
    stack = ReviewStack(
        tmp_path,
        [propose_step("they said the budget is fine", (JOINT_RESOURCE,))],
    )
    seed_five_classes(stack)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id)

    assert result.outcome.kind == OutcomeKind.HELD
    assert "INVALID_REFERENCE" in _outcome_codes(result)
    assert _record_count(stack, "MemoryRecord") == 0


def test_propose_memory_refuses_broad_scope_and_durable_jump(tmp_path: Path) -> None:
    """BROAD scope needs a curated permission that does not exist, and a new
    DURABLE proposal skips the CANDIDATE stage: both are typed holds."""
    for step in (
        propose_step("everything about everyone", ("fragment:evt:1",), scope_kind=ScopeKind.BROAD),
        propose_step("a durable fact", ("fragment:evt:1",), stage=MemoryStage.DURABLE),
    ):
        case_dir = tmp_path / f"case-{step.requested_stage.value}-{step.requested_scope_kind.value}"
        case_dir.mkdir()
        stack = ReviewStack(case_dir, [step])
        seed_five_classes(stack)
        feed_event(stack)
        work_id = stack.coordinator.ready_work_ids()[0]
        result = stack.coordinator.run_turn(work_id)
        assert result.outcome.kind == OutcomeKind.HELD
        assert "POLICY_FORBIDDEN" in _outcome_codes(result)
        assert _record_count(stack, "MemoryRecord") == 0


def test_request_access_retargeting_the_audience_is_a_typed_hold(tmp_path: Path) -> None:
    """The model cannot bind an access request to another audience: the
    audience is runtime-supplied, and a retargeting attempt is CONTEXT_MISMATCH."""
    stack = ReviewStack(
        tmp_path,
        [
            access_step(
                resource_id=FINANCE_RESOURCE,
                audience_ref={"id": "audience:room:dm", "revision": 1},
            )
        ],
    )
    seed_five_classes(stack)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id)

    assert result.outcome.kind == OutcomeKind.HELD
    assert "CONTEXT_MISMATCH" in _outcome_codes(result)
    assert _record_count(stack, "MemoryAccessRequest") == 0
