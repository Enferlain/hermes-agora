"""Fixtures for review routing and the REQUIRED protocol (M2 tasks 4.1/4.2).

A whole-turn stack wired like tests.test_wiring_turn but with a scripted
reviewer on the gate and fixture-configured advisory concerns, plus record
builders for gate-level routing tests: snapshots with explicit inherited
commitments and authorized comparison views, intents, ACT/QUIET plans, and
a STRONG restriction seeded through the trusted manifest registries port
(the fixtures_m2 five resource classes).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agora.commit_gate import CommitGate, GateContext
from agora.context_memory import ContextMemory
from agora.contracts import (
    AssertionStatus,
    ContentFragment,
    ContextSnapshot,
    Disposition,
    Effect,
    EffectKind,
    EffectPlan,
    EligibilityStatus,
    ExposureClass,
    FragmentKind,
    InstructionAuthority,
    Integrity,
    Intent,
    IntentState,
    MainStepKind,
    MemoryView,
    ModelEffectDraft,
    ModelSubmitPlanStep,
    ProducerCoverage,
    ProducerKind,
    Provenance,
    RecordRef,
    RecordStatus,
    RequestedPrecision,
    SnapshotStatus,
    SourceKind,
    canonical_digest,
    canonical_json_bytes,
    render_send_message,
)
from agora.coordinator import Coordinator
from agora.fake_sidecar import FakeSidecar, FakeWorld
from agora.ports import (
    FaultInjector,
    ReviewAssessmentData,
    ScriptedMainModel,
    ScriptedReviewer,
    VirtualClock,
)
from agora.runner import RunManifest, seed_registries
from agora.stores import JournalEntry, TransportStore
from tests.fixtures_m2 import five_class_registries, manifest_with_registries

ANCHOR = "2026-10-12T12:00:00Z"


class ReviewStack:
    """The whole-turn stack with a scripted reviewer wired into the gate."""

    def __init__(
        self,
        tmp: Path,
        steps: Sequence[Any] = (),
        *,
        assessments: Sequence[ReviewAssessmentData] = (),
        advisory_concerns: Sequence[str] = (),
        reviewer: ScriptedReviewer | None = None,
    ) -> None:
        self.clock = VirtualClock(ANCHOR)
        from agora.stores import CoreStore

        self.core = CoreStore(tmp / "core.sqlite")
        self.transport = TransportStore(tmp / "transport.sqlite")
        self.world = FakeWorld(tmp / "world.sqlite")
        self.injector = FaultInjector()
        identity, audience, capability, sink_ref = Coordinator.bootstrap(
            self.core,
            self.clock,
            destination_id="room:public",
            allowed_destination_ids=("room:public",),
        )
        self.identity, self.audience, self.capability = identity, audience, capability
        self.sink_ref = sink_ref
        self.context_memory = ContextMemory(self.core, self.clock, self.injector)
        self.reviewer = reviewer if reviewer is not None else ScriptedReviewer(assessments)
        self.gate = CommitGate(
            self.core,
            self.clock,
            reviewer=self.reviewer,
            advisory_concerns=advisory_concerns,
        )
        self.sidecar = FakeSidecar(self.core, self.transport, self.world, self.clock, self.injector)
        self.coordinator = Coordinator(
            self.core,
            self.clock,
            self.context_memory,
            ScriptedMainModel(steps),
            self.gate,
            self.sidecar,
            identity=identity,
            audience=audience,
            capability=capability,
            sink_ref=sink_ref,
            injector=self.injector,
        )


def seed_five_classes(stack: ReviewStack) -> None:
    """Seed the five handoff resource classes through the manifest port:
    the finance resource carries the STRONG no-expression commitment, and
    use:finance-fields authorizes FIELDS reads for comparison views."""
    manifest = RunManifest.from_payload(manifest_with_registries(five_class_registries()))
    seed_registries(stack.core, stack.clock, manifest.registries)


def gate_context(stack: ReviewStack) -> GateContext:
    return GateContext(
        identity_ref=RecordRef(id=stack.identity.id, revision=stack.identity.revision),
        capability=stack.capability,
        audience=stack.audience,
        max_message_chars=2000,
    )


def make_intent(
    stack: ReviewStack,
    intent_id: str,
    *,
    purpose_class: str = "social-participation",
    goal_fragment_id: str | None = None,
) -> RecordRef:
    """A grantedless agent intent (I01). Without ``goal_fragment_id`` its
    goal fragment carries no commitments; with it, the intent derives from
    an existing fragment — e.g. the restricted finance resource — so the
    plan's derived intent carries that fragment's commitments."""
    now = stack.clock.now_rfc3339()
    if goal_fragment_id is not None:
        goal_ref = RecordRef(id=goal_fragment_id, revision=1)
    else:
        goal_payload = f"payload:goal:{intent_id}"
        stack.core.put_payload(goal_payload, f"goal {intent_id}", owner_ref="identity:agent")
        fragment = ContentFragment(
            id=f"fragment:goal:{intent_id}",
            revision=1,
            created_at=now,
            kind=FragmentKind.TEXT,
            payload_ref=goal_payload,
            provenance=_provenance(stack),
            commitment_refs=(),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
        stack.core.put_record(fragment)
        goal_ref = RecordRef(id=fragment.id, revision=fragment.revision)
    intent = Intent(
        id=intent_id,
        revision=1,
        created_at=now,
        creator_ref=RecordRef(id="principal:agent", revision=1),
        goal_fragment_ref=goal_ref,
        purpose_class=purpose_class,
        observed_trigger_refs=(),
        endorsement_revision=1,
        producer_coverage_ref=None,
        runtime_task_root_ids=(f"root:{intent_id}",),
        parent_work_ref=None,
        allowed_destination_refs=("room:public",),
        due_at=None,
        expires_at=None,
        state=IntentState.OPEN,
    )
    stack.core.put_record(intent)
    return RecordRef(id=intent.id, revision=intent.revision)


def review_snapshot(
    stack: ReviewStack,
    call_id: str,
    intent_ref: RecordRef,
    *,
    selected_fragment_refs: tuple[RecordRef, ...] = (),
    inherited_commitment_refs: tuple[RecordRef, ...] = (),
    views: tuple[MemoryView, ...] = (),
    effect_capable: bool = True,
) -> ContextSnapshot:
    """Assemble + persist a snapshot with explicit inherited commitments
    and active views — the M2 fields ContextMemory.build_snapshot does not
    populate yet (view wiring arrives with tasks 2.2/5.1). The coverage,
    the views, and the snapshot cite each other, so they insert as one
    typed-ref-verified batch."""
    now = stack.clock.now_rfc3339()
    view_refs = tuple(RecordRef(id=view.id, revision=view.revision) for view in views)
    coverage = ProducerCoverage(
        id=f"coverage:context:{call_id}",
        revision=1,
        created_at=now,
        producer_kind=ProducerKind.CONTEXT_ASSEMBLY,
        input_fragment_refs=selected_fragment_refs,
        input_view_refs=view_refs,
        input_intent_refs=(intent_ref,),
        input_policy_refs=(),
        sink_ref=stack.sink_ref,
        call_id=call_id,
        runtime_epoch=stack.core.runtime_epoch,
    )
    snapshot = ContextSnapshot(
        id=f"snapshot:{call_id}",
        revision=1,
        created_at=now,
        identity_ref=RecordRef(id=stack.identity.id, revision=stack.identity.revision),
        intent_ref=intent_ref,
        audience_ref=RecordRef(id=stack.audience.id, revision=stack.audience.revision),
        episode_ref=None,
        selected_fragment_refs=selected_fragment_refs,
        active_view_refs=view_refs,
        producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
        exposure_class=ExposureClass.ORDINARY,
        inherited_commitment_refs=inherited_commitment_refs,
        version_vector=tuple(sorted(selected_fragment_refs, key=lambda ref: ref.id)),
        sink_ref=stack.sink_ref,
        effect_capable=effect_capable,
        serializer_version="ser:v1",
        status=SnapshotStatus.ACTIVE,
    )
    stack.core.put_records((coverage, *views, snapshot))
    return snapshot


def comparison_view(
    stack: ReviewStack,
    view_id: str,
    *,
    call_id: str,
    fragment_ref: RecordRef,
    authorization_id: str,
    inherited_commitment_refs: tuple[RecordRef, ...] = (),
) -> MemoryView:
    """An (unpersisted) authorized FIELDS view over a restricted fragment:
    the reviewer's comparison context for a REQUIRED route. Its producer
    coverage is the context coverage ``review_snapshot`` will build for
    ``call_id``; ``review_snapshot`` batch-inserts the view."""
    return MemoryView(
        id=view_id,
        revision=1,
        created_at=stack.clock.now_rfc3339(),
        resource_id=fragment_ref.id,
        returned_fragment_refs=(fragment_ref,),
        authorization_refs=(RecordRef(id=authorization_id, revision=1),),
        transform_certificate_ref=None,
        producer_coverage_ref=RecordRef(id=f"coverage:context:{call_id}", revision=1),
        precision=RequestedPrecision.FIELDS,
        inherited_commitment_refs=inherited_commitment_refs,
        permitted_uses=(),
        audience_bounds=("room:public",),
        valid_until=None,
        status=RecordStatus.ACTIVE,
    )


def speak_plan(
    stack: ReviewStack,
    plan_id: str,
    *,
    snapshot: ContextSnapshot,
    intent_ref: RecordRef,
    text: str = "a careful public note",
    proposal_revision: int = 1,
) -> EffectPlan:
    """Persist a SEND_MESSAGE effect and an ACT plan citing it, mirroring
    the coordinator's rendering and digest computation."""
    now = stack.clock.now_rfc3339()
    rendered = render_send_message(destination_id="room:public", text=text, reply_target_ref=None)
    payload_id = f"payload:effect:{plan_id}"
    stack.core.put_payload(
        payload_id, canonical_json_bytes(rendered).decode("utf-8"), owner_ref=f"plan:{plan_id}"
    )
    effect = Effect(
        id=f"{plan_id}:effect:0",
        revision=1,
        created_at=now,
        logical_effect_id=f"{plan_id}:effect:0",
        kind=EffectKind.SEND_MESSAGE,
        account_id=stack.capability.account_id,
        destination_id="room:public",
        target_ref=None,
        rendered_payload_ref=payload_id,
        payload_digest=canonical_digest(rendered),
        capability_ref=RecordRef(id=stack.capability.id, revision=stack.capability.revision),
    )
    stack.core.put_record(effect)
    plan = EffectPlan(
        id=plan_id,
        revision=1,
        created_at=now,
        intent_ref=intent_ref,
        snapshot_ref=RecordRef(id=snapshot.id, revision=snapshot.revision),
        disposition=Disposition.ACT,
        effects=(effect,),
        proposal_revision=proposal_revision,
        proposal_digest=canonical_digest(
            {
                "disposition": Disposition.ACT.value,
                "effects": [effect.payload_digest],
                "intent": intent_ref.to_payload(),
            }
        ),
        defer_until=None,
        defer_expires_at=None,
        declared_fact_refs=(),
        observed_target_refs=(),
        producer_coverage_ref=RecordRef(
            id=snapshot.producer_coverage_ref.id, revision=snapshot.producer_coverage_ref.revision
        ),
    )
    stack.core.put_record(plan)
    return plan


def quiet_plan(
    stack: ReviewStack,
    plan_id: str,
    *,
    snapshot: ContextSnapshot,
    intent_ref: RecordRef,
    proposal_revision: int = 1,
) -> EffectPlan:
    """Persist a zero-effect QUIET plan (the M1 shortcut path, now routed)."""
    plan = EffectPlan(
        id=plan_id,
        revision=1,
        created_at=stack.clock.now_rfc3339(),
        intent_ref=intent_ref,
        snapshot_ref=RecordRef(id=snapshot.id, revision=snapshot.revision),
        disposition=Disposition.QUIET,
        effects=(),
        proposal_revision=proposal_revision,
        proposal_digest=canonical_digest(
            {
                "disposition": Disposition.QUIET.value,
                "effects": [],
                "intent": intent_ref.to_payload(),
            }
        ),
        defer_until=None,
        defer_expires_at=None,
        declared_fact_refs=(),
        observed_target_refs=(),
        producer_coverage_ref=RecordRef(
            id=snapshot.producer_coverage_ref.id, revision=snapshot.producer_coverage_ref.revision
        ),
    )
    stack.core.put_record(plan)
    return plan


def speak_step(text: str) -> ModelSubmitPlanStep:
    return ModelSubmitPlanStep(
        kind=MainStepKind.SUBMIT_PLAN,
        intent_ref=RecordRef(id="intent:turn:work:turn:evt:1", revision=1),
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


def feed_event(
    stack: ReviewStack,
    seq: int = 1,
    *,
    fragment_id: str | None = None,
) -> None:
    """Ingest one journal event: an ordinary text fragment by default, or
    an existing seeded fragment (e.g. the restricted finance resource) by
    id — its commitments then travel through runtime producer coverage."""
    if fragment_id is None:
        fragment_id = f"fragment:evt:{seq}"
        payload_id = f"payload:evt:{seq}"
        stack.core.put_payload(payload_id, f"hello hermes {seq}", owner_ref=f"evt:{seq}")
        stack.core.put_record(
            ContentFragment(
                id=fragment_id,
                revision=1,
                created_at=stack.clock.now_rfc3339(),
                kind=FragmentKind.TEXT,
                payload_ref=payload_id,
                provenance=_provenance(stack),
                commitment_refs=(),
                eligibility_status=EligibilityStatus.ACTIVE,
            )
        )
    stack.transport.append_events(
        (
            JournalEntry(
                journal_seq=seq,
                source_event_id=f"evt:{seq}",
                source_revision=1,
                kind="MESSAGE_CREATED",
                payload_ref=f"payload:evt:{seq}",
                fragment_refs=(fragment_id,),
                recovery="COMPLETE",
            ),
        )
    )
    stack.coordinator.ingest(stack.transport, consumer_id="core-1")


def _provenance(stack: ReviewStack) -> Provenance:
    now = stack.clock.now_rfc3339()
    return Provenance(
        producer_ref=RecordRef(id="principal:agent", revision=1),
        source_kind=SourceKind.AGENT_OUTPUT,
        origin_event_ref=None,
        claimed_speaker_ref=None,
        instruction_authority=InstructionAuthority.AGENT_STATE,
        integrity=Integrity.DERIVED,
        custodian_refs=(),
        subject_refs=(),
        original_audience_ref=None,
        assertion_status=AssertionStatus.AGENT_ADOPTED_PREFERENCE,
        producer_coverage_ref=None,
        model_claimed_parent_refs=(),
        occurred_at=now,
        observed_at=now,
    )
