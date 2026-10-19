"""Whole-turn wiring tests: the deterministic end-to-end path (M1 task 5.1/5.2).

coordinator → context/memory → scripted main → CommitGate → fake
sidecar/world, on the real two-domain stores. Demonstrates: speak, react,
explicit no action, unsupported stays inert, budgets, and crash-between-
model-and-accept recovery.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from agora.commit_gate import CommitGate
from agora.context_memory import ContextMemory
from agora.contracts import (
    AgentIdentity,
    AssertionStatus,
    AudienceSnapshot,
    CapabilityEnvelope,
    ContentFragment,
    DecisionKind,
    DeliveryReceipt,
    Disposition,
    EffectKind,
    EligibilityStatus,
    FinishChoice,
    FragmentKind,
    InstructionAuthority,
    Integrity,
    MainStep,
    MainStepKind,
    ModelFinishStep,
    ModelSubmitPlanStep,
    OutcomeKind,
    Provenance,
    RecordRef,
    SourceKind,
)
from agora.coordinator import Coordinator
from agora.fake_sidecar import FakeSidecar, FakeWorld
from agora.ports import (
    CrashCheckpoint,
    FaultInjector,
    InjectedCrash,
    ScriptedMainModel,
    VirtualClock,
)
from agora.stores import JournalEntry, TransportStore

ANCHOR = "2026-10-10T12:00:00Z"


class Stack:
    """The full M1 stack wired over fresh stores in a temp directory."""

    def __init__(self, tmp: Path, steps: Sequence[MainStep]) -> None:
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
        self.context_memory = ContextMemory(self.core, self.clock, self.injector)
        self.main_port = ScriptedMainModel(steps)
        self.gate = CommitGate(self.core, self.clock)
        self.sidecar = FakeSidecar(self.core, self.transport, self.world, self.clock, self.injector)
        self.coordinator = Coordinator(
            self.core,
            self.clock,
            self.context_memory,
            self.main_port,
            self.gate,
            self.sidecar,
            identity=identity,
            audience=audience,
            capability=capability,
            sink_ref=sink_ref,
            injector=self.injector,
        )


def speak_step(text: str) -> ModelSubmitPlanStep:
    from agora.contracts import ModelEffectDraft

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


def feed_event(stack: Stack, seq: int = 1) -> None:
    stack.transport.append_events(
        (
            JournalEntry(
                journal_seq=seq,
                source_event_id=f"evt:{seq}",
                source_revision=1,
                kind="MESSAGE_CREATED",
                payload_ref=f"payload:evt:{seq}",
                fragment_refs=(f"fragment:evt:{seq}",),
                recovery="COMPLETE",
            ),
        )
    )
    stack.core.put_payload(f"payload:evt:{seq}", f"hello hermes {seq}", owner_ref=f"evt:{seq}")
    stack.core.put_record(
        ContentFragment(
            id=f"fragment:evt:{seq}",
            revision=1,
            created_at=stack.clock.now_rfc3339(),
            kind=FragmentKind.TEXT,
            payload_ref=f"payload:evt:{seq}",
            provenance=Provenance(
                producer_ref=RecordRef(id="principal:agent", revision=1),
                source_kind=SourceKind.SOCIAL_MESSAGE,
                origin_event_ref=None,
                claimed_speaker_ref=None,
                instruction_authority=InstructionAuthority.NONE,
                integrity=Integrity.EXTERNAL_UNVERIFIED,
                custodian_refs=(),
                subject_refs=(),
                original_audience_ref=None,
                assertion_status=AssertionStatus.OBSERVED_STATEMENT,
                producer_coverage_ref=None,
                model_claimed_parent_refs=(),
                occurred_at=stack.clock.now_rfc3339(),
                observed_at=stack.clock.now_rfc3339(),
            ),
            commitment_refs=(),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
    )
    stack.coordinator.ingest(stack.transport, consumer_id="core-1")


def test_speak_turn_commits_exact_bytes(tmp_path: Path) -> None:
    stack = Stack(tmp_path, [speak_step("That reminds me of something useful from our project.")])
    feed_event(stack)
    work_ids = stack.coordinator.ready_work_ids()
    assert len(work_ids) == 1
    result = stack.coordinator.run_turn(work_ids[0])

    assert result.outcome.kind == OutcomeKind.COMMITTED
    assert result.decision is not None and result.decision.kind == DecisionKind.AUTHORIZED
    assert result.receipt_ref is not None
    receipt = stack.core.get_record(result.receipt_ref.id, result.receipt_ref.revision)
    assert isinstance(receipt, DeliveryReceipt) and receipt.state.value == "SENT"

    attempt = stack.transport._conn.execute(
        "SELECT attempt_id FROM transport_attempts ORDER BY started_at_ms DESC LIMIT 1"
    ).fetchone()
    observed = stack.world.visible_bytes(attempt["attempt_id"])
    assert observed is not None
    assert b'"text":"That reminds me of something useful from our project."' in observed
    assert b'"destination_id":"room:public"' in observed
    assert stack.transport.outbox_state(f"{work_ids[0]}:effect:0") == "SENT"


def test_quiet_turn_creates_no_outcome_noise(tmp_path: Path) -> None:
    stack = Stack(
        tmp_path, [ModelFinishStep(kind=MainStepKind.FINISH, outcome=FinishChoice.NO_ACTION)]
    )
    feed_event(stack)
    result = stack.coordinator.run_turn(stack.coordinator.ready_work_ids()[0])
    assert result.outcome.kind == OutcomeKind.NO_ACTION
    # silence: no outbox row, no attempts, no world visibility
    assert stack.transport._conn.execute("SELECT count(*) FROM transport_outbox").fetchone()[0] == 0
    assert (
        stack.transport._conn.execute("SELECT count(*) FROM transport_attempts").fetchone()[0] == 0
    )
    assert stack.world._conn.execute("SELECT count(*) FROM world_effects").fetchone()[0] == 0


def test_unsupported_destination_is_denied_and_inert(tmp_path: Path) -> None:
    from agora.contracts import ModelEffectDraft

    step = ModelSubmitPlanStep(
        kind=MainStepKind.SUBMIT_PLAN,
        intent_ref=RecordRef(id="intent:turn:work:turn:evt:1", revision=1),
        disposition=Disposition.ACT,
        effects=(
            ModelEffectDraft(
                kind=EffectKind.ADD_REACTION,
                destination_id="room:secret",
                text=None,
                target_ref=RecordRef(id="fragment:evt:1", revision=1),
                emoji="👋",
            ),
        ),
        declared_fact_refs=(),
    )
    stack = Stack(tmp_path, [step])
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    result = stack.coordinator.run_turn(work_id)
    assert result.outcome.kind == OutcomeKind.HELD
    assert result.decision is not None
    assert "CAPABILITY_INVALID" in [code.value for code in result.decision.reason_codes]
    assert stack.transport._conn.execute("SELECT count(*) FROM transport_outbox").fetchone()[0] == 0


def test_budget_exhaustion_ends_quietly(tmp_path: Path) -> None:
    from agora.contracts import ModelParticipateStep, ParticipateMode

    steps = [
        ModelParticipateStep(
            kind=MainStepKind.PARTICIPATE, mode=ParticipateMode.EXPLORE, interest_updates=()
        )
        for _ in range(8)
    ]
    stack = Stack(tmp_path, steps)
    feed_event(stack)
    result = stack.coordinator.run_turn(stack.coordinator.ready_work_ids()[0])
    assert result.outcome.kind == OutcomeKind.FAILED_INTERNAL
    assert "BUDGET_EXHAUSTED" in [code.value for code in result.outcome.reason_codes]
    assert stack.transport._conn.execute("SELECT count(*) FROM transport_outbox").fetchone()[0] == 0


def test_model_supplied_bad_reference_is_a_typed_denial(tmp_path: Path) -> None:
    """The coordinator validates referenced ids (§5): a reaction citing a
    missing target is HELD with INVALID_REFERENCE and writes nothing."""
    from agora.contracts import ModelEffectDraft

    step = ModelSubmitPlanStep(
        kind=MainStepKind.SUBMIT_PLAN,
        intent_ref=RecordRef(id="intent:turn:work:turn:evt:1", revision=1),
        disposition=Disposition.ACT,
        effects=(
            ModelEffectDraft(
                kind=EffectKind.ADD_REACTION,
                destination_id="room:public",
                text=None,
                target_ref=RecordRef(id="fragment:evt:404", revision=1),
                emoji="👋",
            ),
        ),
        declared_fact_refs=(),
    )
    stack = Stack(tmp_path, [step])
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    result = stack.coordinator.run_turn(work_id)
    assert result.outcome.kind == OutcomeKind.HELD
    assert "INVALID_REFERENCE" in [code.value for code in result.outcome.reason_codes]
    # nothing written past the denial: no effect, plan, outbox, or world rows
    assert stack.core.get_record(f"{work_id}:effect:0") is None
    assert stack.core.get_record(f"plan:{work_id}:1") is None
    assert stack.transport._conn.execute("SELECT count(*) FROM transport_outbox").fetchone()[0] == 0
    assert stack.world._conn.execute("SELECT count(*) FROM world_effects").fetchone()[0] == 0


def test_crash_between_model_and_accept_recovers_deterministically(tmp_path: Path) -> None:
    stack = Stack(tmp_path, [speak_step("recovered turn")])
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    stack.injector.arm(CrashCheckpoint.AFTER_MODEL_BEFORE_ACCEPT)
    with pytest.raises(InjectedCrash):
        stack.coordinator.run_turn(work_id)

    # Simulated process death: reopen everything; work recovered to READY.
    stack.core.close()
    stack.transport.close()
    stack.world.close()
    clock = VirtualClock(ANCHOR, start_ms=stack.clock.now_ms())
    from agora.stores import CoreStore

    core2 = CoreStore(tmp_path / "core.sqlite")
    transport2 = TransportStore(tmp_path / "transport.sqlite")
    world2 = FakeWorld(tmp_path / "world.sqlite")
    injector2 = FaultInjector()
    context2 = ContextMemory(core2, clock, injector2)
    # the scripted model replays the same step at the same index
    main2 = ScriptedMainModel([speak_step("recovered turn")])
    identity = core2.get_record("identity:agent")
    audience = core2.get_record("audience:room:public")
    capability = core2.get_record("capability:social")
    assert isinstance(identity, AgentIdentity)
    assert isinstance(audience, AudienceSnapshot)
    assert isinstance(capability, CapabilityEnvelope)
    coordinator2 = Coordinator(
        core2,
        clock,
        context2,
        main2,
        CommitGate(core2, clock),
        FakeSidecar(core2, transport2, world2, clock, injector2),
        identity=identity,
        audience=audience,
        capability=capability,
        sink_ref=RecordRef(id="sink:scripted", revision=1),
        injector=injector2,
    )
    ready = coordinator2.ready_work_ids()
    assert ready == (work_id,)
    work = core2.get_work(work_id)
    assert work is not None and work["main_steps_used"] == 0  # step was not accepted
    result = coordinator2.run_turn(work_id)
    assert result.outcome.kind == OutcomeKind.COMMITTED
    # exactly one step was ever accepted
    final_work = core2.get_work(work_id)
    assert final_work is not None and final_work["main_steps_used"] == 1
    assert transport2.outbox_state(f"{work_id}:effect:0") == "SENT"


def test_reaction_to_purged_target_payload_is_a_typed_hold(tmp_path: Path) -> None:
    """The reaction's content digest binds real target bytes (bead
    hermes-agora-ecy): a purged target payload cannot be digested, so the
    turn holds with INVALID_REFERENCE — never a placeholder digest and
    never a dispatched reaction."""
    from agora.contracts import Disposition, ModelEffectDraft, ModelSubmitPlanStep

    step = ModelSubmitPlanStep(
        kind=MainStepKind.SUBMIT_PLAN,
        intent_ref=RecordRef(id="intent:turn:work:turn:evt:1", revision=1),
        disposition=Disposition.ACT,
        effects=(
            ModelEffectDraft(
                kind=EffectKind.ADD_REACTION,
                destination_id="room:public",
                text=None,
                target_ref=RecordRef(id="fragment:evt:1", revision=1),
                emoji="👋",
            ),
        ),
        declared_fact_refs=(),
    )
    stack = Stack(tmp_path, [step])
    feed_event(stack)
    stack.core._conn.execute(
        "UPDATE core_payloads SET status = 'PURGED' WHERE payload_id = 'payload:evt:1'"
    )
    work_id = stack.coordinator.ready_work_ids()[0]
    result = stack.coordinator.run_turn(work_id)
    assert result.outcome.kind == OutcomeKind.HELD
    assert "INVALID_REFERENCE" in [code.value for code in result.outcome.reason_codes]
    # nothing written past the denial: no effect, plan, outbox, or world rows
    assert stack.core.get_record(f"{work_id}:effect:0") is None
    assert stack.core.get_record(f"plan:{work_id}:1") is None
    assert stack.transport._conn.execute("SELECT count(*) FROM transport_outbox").fetchone()[0] == 0
    assert stack.world._conn.execute("SELECT count(*) FROM world_effects").fetchone()[0] == 0


def test_defer_via_submit_plan_defers_the_intent(tmp_path: Path) -> None:
    """Review finding: a direct SUBMIT_PLAN(DEFER) resolution is honored
    like the FINISH(DEFER) path — the intent gets its due time and the
    turn ends DEFERRED, never silently swallowed."""
    from agora.contracts import Disposition, ModelSubmitPlanStep

    defer_step = ModelSubmitPlanStep(
        kind=MainStepKind.SUBMIT_PLAN,
        intent_ref=RecordRef(id="intent:turn:work:turn:evt:1", revision=1),
        disposition=Disposition.DEFER,
        effects=(),
        declared_fact_refs=(),
    )
    stack = Stack(tmp_path, [defer_step])
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    result = stack.coordinator.run_turn(work_id)
    assert result.outcome.kind == OutcomeKind.DEFERRED
    intent = stack.core.get_record(f"intent:turn:{work_id}")
    from agora.contracts import Intent

    assert isinstance(intent, Intent) and intent.due_at is not None


def test_malformed_access_request_is_a_typed_hold_not_a_crash(tmp_path: Path) -> None:
    """Review finding: model-reachable request fields that fail the record
    contract (a list resource_id, an id-hostile request_id) are typed
    INVALID_SCHEMA holds that complete the work — never an exception out
    of the turn stranding it RUNNING with a live lease."""
    from agora.contracts import MainStepKind as MSK
    from agora.contracts import ModelRequestAccessStep

    stack = Stack(
        tmp_path,
        [
            ModelRequestAccessStep(
                kind=MSK.REQUEST_ACCESS,
                request={
                    "resource_id": ["not-a-string"],
                    "intended_use": "INTERNAL_ONLY",
                    "requested_precision": "FIELDS",
                },
            ),
            ModelRequestAccessStep(
                kind=MSK.REQUEST_ACCESS,
                request={
                    "request_id": "bad id!",
                    "resource_id": None,
                    "data_class": "x",
                    "intended_use": "INTERNAL_ONLY",
                    "requested_precision": "FIELDS",
                },
            ),
        ],
    )
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    result = stack.coordinator.run_turn(work_id)
    assert result.outcome.kind == OutcomeKind.HELD
    assert "INVALID_SCHEMA" in [code.value for code in result.outcome.reason_codes]
    work = stack.core.get_work(work_id)
    assert work is not None and work["state"] == "COMPLETED"  # not stranded RUNNING


def test_repeated_create_intent_with_the_same_goal_never_crashes(tmp_path: Path) -> None:
    """Review finding: the goal stem is uniquified per creation, so the
    same goal text can ground a second intent instead of colliding on the
    shared fragment/intent ids."""
    from agora.contracts import ModelCreateIntentStep

    step = ModelCreateIntentStep(
        kind=MainStepKind.CREATE_INTENT,
        goal_text="track the project thread",
        purpose_class="social-participation",
        allowed_destination_ids=("room:public",),
        due_at=None,
        expires_at=None,
    )
    stack = Stack(tmp_path, [step, step])
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    stack.coordinator.run_turn(work_id)  # the turn must not crash
    # the exhausted-script outcome shape is the M1 convention
    # (FAILED_INTERNAL, and with the audit fix no longer mislabeled budget
    # exhaustion); the point is both intents grounded
    stems = stack.core._conn.execute(
        "SELECT record_id FROM core_records WHERE record_id LIKE 'intent:agent:%'"
    ).fetchall()
    assert len(stems) == 2  # both creations grounded distinct intents


def test_timeout_delivery_reports_unknown_not_committed(tmp_path: Path) -> None:
    """Review finding: a dispatch that ends UNKNOWN is reported as
    DELIVERY_UNKNOWN — reconciliation owns the resolution; the turn never
    claims a possibly-delivered effect as committed."""
    from tests.fixtures_review import speak_step

    stack = Stack(tmp_path, [speak_step("into the void")])
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    stack.world.script(
        f"attempt:{work_id}:effect:0:1",
        __import__("agora.fake_sidecar", fromlist=["WorldOutcome"]).WorldOutcome("timeout"),
    )
    result = stack.coordinator.run_turn(work_id)
    assert result.outcome.kind == OutcomeKind.DELIVERY_UNKNOWN
    assert "DELIVERY_UNKNOWN" in [code.value for code in result.outcome.reason_codes]
