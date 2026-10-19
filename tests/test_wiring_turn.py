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
    canonical_digest,
)
from agora.coordinator import Coordinator, TurnConfig
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

    def __init__(
        self,
        tmp: Path,
        steps: Sequence[MainStep],
        *,
        start_calls: int = 0,
        start_ms: int = 0,
        config: TurnConfig | None = None,
    ) -> None:
        self.clock = VirtualClock(ANCHOR, start_ms=start_ms)
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
        self.main_port = ScriptedMainModel(steps, start_calls=start_calls)
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
            config=config,
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
    like the FINISH(DEFER) path — the intent gets the model's supplied
    window and the turn ends DEFERRED, never silently swallowed."""
    from agora.contracts import Disposition, Intent, ModelSubmitPlanStep

    defer_step = ModelSubmitPlanStep(
        kind=MainStepKind.SUBMIT_PLAN,
        intent_ref=RecordRef(id="intent:turn:work:turn:evt:1", revision=1),
        disposition=Disposition.DEFER,
        effects=(),
        declared_fact_refs=(),
        defer_until="2026-10-10T12:05:00Z",
        defer_expires_at="2026-10-10T12:30:00Z",
    )
    stack = Stack(tmp_path, [defer_step])
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    result = stack.coordinator.run_turn(work_id)
    assert result.outcome.kind == OutcomeKind.DEFERRED
    intent = stack.core.get_record(f"intent:turn:{work_id}")
    assert isinstance(intent, Intent) and intent.state.value == "DEFERRED"
    # the model's supplied window is what got stamped — a real future due,
    # not a defer that expires the instant it is created
    assert intent.due_at == "2026-10-10T12:05:00Z"
    assert intent.expires_at == "2026-10-10T12:30:00Z"


def _defer_step(defer_until: str, defer_expires_at: str) -> ModelSubmitPlanStep:
    from agora.contracts import Disposition

    return ModelSubmitPlanStep(
        kind=MainStepKind.SUBMIT_PLAN,
        intent_ref=RecordRef(id="intent:turn:work:turn:evt:1", revision=1),
        disposition=Disposition.DEFER,
        effects=(),
        declared_fact_refs=(),
        defer_until=defer_until,
        defer_expires_at=defer_expires_at,
    )


def test_finish_defer_uses_the_runtime_window(tmp_path: Path) -> None:
    """FINISH carries only the DEFER choice, so the runtime mints the
    fixed-time window (§6.4): due at now + defer_window_ms, expiry at due
    + defer_expiry_ms — a real future window, never a defer that expires
    the instant it is created (bead 9xk)."""
    from agora.contracts import EffectPlan, FinishChoice, Intent, ModelFinishStep

    stack = Stack(tmp_path, [ModelFinishStep(kind=MainStepKind.FINISH, outcome=FinishChoice.DEFER)])
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    result = stack.coordinator.run_turn(work_id)
    assert result.outcome.kind == OutcomeKind.DEFERRED
    intent = stack.core.get_record(f"intent:turn:{work_id}")
    assert isinstance(intent, Intent) and intent.state.value == "DEFERRED"
    # the clock sits at the anchor (12:00:00): +60s due, +300s expiry
    assert intent.due_at == "2026-10-10T12:01:00Z"
    assert intent.expires_at == "2026-10-10T12:06:00Z"
    plan = stack.core.get_record(f"plan:{work_id}:1")
    assert isinstance(plan, EffectPlan)
    assert plan.defer_until == "2026-10-10T12:01:00Z"
    assert plan.defer_expires_at == "2026-10-10T12:06:00Z"


def test_timer_wake_resumes_the_deferred_intent(tmp_path: Path) -> None:
    """Bead 9xk: the timer occurrence is a deduped TIMER_DUE journal event
    whose work row binds to the deferred intent; the wake turn resumes
    that intent DEFERRED → OPEN and runs under it, so the deferred goal's
    commitments stay in the wake turn's routing inputs instead of silently
    vanishing under a fresh mechanical intent."""
    from agora.contracts import FinishChoice, Intent, ModelFinishStep

    stack = Stack(
        tmp_path,
        [
            _defer_step("2026-10-10T12:05:00Z", "2026-10-10T12:30:00Z"),
            ModelFinishStep(kind=MainStepKind.FINISH, outcome=FinishChoice.NO_ACTION),
        ],
    )
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    assert stack.coordinator.run_turn(work_id).outcome.kind == OutcomeKind.DEFERRED
    intent_id = f"intent:turn:{work_id}"

    # the timer fires (runner shape): one TIMER_DUE journal occurrence
    timer_event_id = f"timer:{intent_id}:2026-10-10T12:05:00Z:1"
    stack.transport.append_events(
        (
            JournalEntry(
                journal_seq=2,
                source_event_id=timer_event_id,
                source_revision=1,
                kind="TIMER_DUE",
                payload_ref=f"payload:{timer_event_id}",
                fragment_refs=(),
                recovery="COMPLETE",
            ),
        )
    )
    stack.coordinator.ingest(stack.transport, consumer_id="core-1")

    wake_ids = stack.coordinator.ready_work_ids()
    assert len(wake_ids) == 1
    wake_work = stack.core.get_work(wake_ids[0])
    assert wake_work is not None and wake_work["intent_ref"] == intent_id

    result = stack.coordinator.run_turn(wake_ids[0])
    assert result.outcome.kind == OutcomeKind.NO_ACTION
    # the wake ran under the RESUMED intent (OPEN again), not a fresh one
    assert result.outcome.intent_ref is not None
    assert result.outcome.intent_ref.id == intent_id
    intent = stack.core.get_record(intent_id)
    assert isinstance(intent, Intent) and intent.state.value == "OPEN"
    assert result.outcome.intent_ref.revision == intent.revision


def test_defer_expiry_preceding_due_is_a_typed_hold(tmp_path: Path) -> None:
    """A defer whose expiry precedes its due can never wake — a typed
    INVALID_SCHEMA rejection, never a silently-dead deferral."""
    stack = Stack(tmp_path, [_defer_step("2026-10-10T12:30:00Z", "2026-10-10T12:05:00Z")])
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    result = stack.coordinator.run_turn(work_id)
    assert result.outcome.kind == OutcomeKind.HELD
    assert "INVALID_SCHEMA" in [code.value for code in result.outcome.reason_codes]
    intent = stack.core.get_record(f"intent:turn:{work_id}")
    from agora.contracts import Intent

    assert isinstance(intent, Intent) and intent.state.value == "OPEN"  # never deferred


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


def test_create_intent_with_unregistered_purpose_class_is_a_typed_hold(tmp_path: Path) -> None:
    """Bead njy: the purpose-class vocabulary is runtime-registered fixture
    policy (handoff §4.5 — bounded strings used to MATCH a permission,
    never evidence for issuing one). A model-invented class is a typed
    POLICY_FORBIDDEN hold that completes the work; no intent record is
    grounded, so no model-supplied string reaches authority evaluation."""
    from agora.contracts import ModelCreateIntentStep

    step = ModelCreateIntentStep(
        kind=MainStepKind.CREATE_INTENT,
        goal_text="get the protected ledger",
        purpose_class="definitely-not-registered",
        allowed_destination_ids=("room:public",),
        due_at=None,
        expires_at=None,
    )
    stack = Stack(tmp_path, [step])
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    result = stack.coordinator.run_turn(work_id)
    assert result.outcome.kind == OutcomeKind.HELD
    assert "POLICY_FORBIDDEN" in [code.value for code in result.outcome.reason_codes]
    agent_intents = stack.core._conn.execute(
        "SELECT count(*) FROM core_records WHERE record_id LIKE 'intent:agent:%'"
    ).fetchone()[0]
    assert agent_intents == 0
    work = stack.core.get_work(work_id)
    assert work is not None and work["state"] == "COMPLETED"


def test_create_intent_with_registered_purpose_class_grounds(tmp_path: Path) -> None:
    """The positive branch: a class from the wired vocabulary grounds the
    intent — registration restricts the vocabulary, it does not require a
    grant (I01/D02: no grant prerequisite for intent creation)."""
    from agora.contracts import ModelCreateIntentStep

    step = ModelCreateIntentStep(
        kind=MainStepKind.CREATE_INTENT,
        goal_text="coordinate the project thread",
        purpose_class="project-coordination",
        allowed_destination_ids=("room:public",),
        due_at=None,
        expires_at=None,
    )
    stack = Stack(
        tmp_path,
        [step],
        config=TurnConfig(
            purpose_classes=frozenset({"social-participation", "project-coordination"})
        ),
    )
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    stack.coordinator.run_turn(work_id)
    grounded = stack.core._conn.execute(
        "SELECT count(*) FROM core_records WHERE record_id LIKE 'intent:agent:%'"
    ).fetchone()[0]
    assert grounded == 1


def test_submit_path_crash_is_atomic_and_recovers(tmp_path: Path) -> None:
    """yp: a crash inside the submit transaction rolls back payloads,
    effects, the plan, and the submissions counter together — the recovery
    turn re-derives the same proposal revision cleanly instead of
    conflicting with orphan rows on every restart (the old crash loop)."""
    stack = Stack(tmp_path, [speak_step("atomic submit")])
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    stack.injector.arm(CrashCheckpoint.DURING_SUBMIT_TX)
    with pytest.raises(InjectedCrash):
        stack.coordinator.run_turn(work_id)
    # all-or-nothing: no orphan effects, plans, rendered bytes, or counter
    # movement
    assert stack.core.get_record(f"{work_id}:effect:1:0") is None
    assert stack.core.get_record(f"plan:{work_id}:1") is None
    assert stack.core.get_payload(f"payload:effect:{work_id}:1:0") is None
    work = stack.core.get_work(work_id)
    assert work is not None and work["submissions_used"] == 0

    # Simulated process death; the accepted submit step never replays, the
    # next scripted step re-derives proposal revision 1 from a clean slate.
    # The clock advances across the restart so the re-derivation cannot
    # ride the byte-identical dedup path.
    stack.clock.advance_ms(50_000)
    start_ms = stack.clock.now_ms()
    stack.core.close()
    stack.transport.close()
    stack.world.close()
    reopened = Stack(
        tmp_path,
        [speak_step("atomic submit"), speak_step("atomic submit")],
        start_calls=1,
        start_ms=start_ms,
    )
    assert reopened.coordinator.ready_work_ids() == (work_id,)
    result = reopened.coordinator.run_turn(work_id)
    assert result.outcome.kind == OutcomeKind.COMMITTED
    work2 = reopened.core.get_work(work_id)
    assert work2 is not None and work2["submissions_used"] == 1
    assert reopened.transport.outbox_state(f"{work_id}:effect:0") == "SENT"


def test_outcome_and_completion_commit_atomically(tmp_path: Path) -> None:
    """yp: the terminal decisions, the fixed-id outcome record, and the
    work completion UPDATE commit as one unit — a crash between the old
    two-transaction writes orphaned outcome:{work}, and the recovery turn's
    rewrite of the same id raised RecordConflictError on every restart.
    The pre-crash dispatch stays durable either way."""
    from agora.contracts import FinishChoice, ModelFinishStep

    steps = [
        speak_step("tail atomicity"),
        ModelFinishStep(kind=MainStepKind.FINISH, outcome=FinishChoice.NO_ACTION),
    ]
    stack = Stack(tmp_path, steps)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    stack.injector.arm(CrashCheckpoint.DURING_COMPLETION_TX)
    with pytest.raises(InjectedCrash):
        stack.coordinator.run_turn(work_id)
    assert stack.core.get_record(f"outcome:{work_id}") is None
    work = stack.core.get_work(work_id)
    assert work is not None and work["state"] == "RUNNING"
    assert work["terminal_outcome_ref"] is None
    # the dispatch committed before the tail: durable across the crash
    assert stack.transport.outbox_state(f"{work_id}:effect:0") == "SENT"

    stack.clock.advance_ms(50_000)
    start_ms = stack.clock.now_ms()
    stack.core.close()
    stack.transport.close()
    stack.world.close()
    reopened = Stack(tmp_path, steps, start_calls=1, start_ms=start_ms)
    result = reopened.coordinator.run_turn(work_id)
    assert result.outcome.kind == OutcomeKind.NO_ACTION
    work2 = reopened.core.get_work(work_id)
    assert work2 is not None and work2["state"] == "COMPLETED"
    assert work2["terminal_outcome_ref"] == f"outcome:{work_id}"
    assert reopened.core.get_record(f"outcome:{work_id}") is not None


def test_intent_mint_crash_rolls_back_cleanly(tmp_path: Path) -> None:
    """yp: the mechanical intent mint (payload + fragment + intent) is one
    transaction — a crash mid-mint leaves nothing behind, so the recovery
    turn mints cleanly instead of surfacing a spurious CONFLICT hold
    against an orphaned fixed-id fragment."""
    stack = Stack(tmp_path, [speak_step("mint recovery")])
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    stack.injector.arm(CrashCheckpoint.DURING_INTENT_MINT)
    with pytest.raises(InjectedCrash):
        stack.coordinator.run_turn(work_id)
    assert stack.core.get_record(f"intent:turn:{work_id}") is None
    orphan_fragments = stack.core._conn.execute(
        "SELECT count(*) FROM core_records WHERE record_id LIKE 'fragment:goal:turn:%'"
    ).fetchone()[0]
    assert orphan_fragments == 0
    goal_payload = f"payload:goal:turn:{canonical_digest(work_id)[:16]}"
    assert stack.core.get_payload(goal_payload) is None
    work = stack.core.get_work(work_id)
    assert work is not None and work["main_steps_used"] == 0

    stack.clock.advance_ms(50_000)
    start_ms = stack.clock.now_ms()
    stack.core.close()
    stack.transport.close()
    stack.world.close()
    reopened = Stack(tmp_path, [speak_step("mint recovery")], start_ms=start_ms)
    result = reopened.coordinator.run_turn(work_id)
    assert result.outcome.kind == OutcomeKind.COMMITTED
    assert reopened.core.get_record(f"intent:turn:{work_id}") is not None
