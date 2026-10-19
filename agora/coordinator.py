"""Coordinator for the M1 slice (handoff §2, §5, §6.1).

Owns event ingestion orchestration, work episodes, main steps with budgets,
and outcome persistence. The coordinator supplies every verified actor,
coverage, and authority field — the model supplies only model-selectable
content. No grant is created from model text; no sidecar table is written
directly. Scripted participation makes silence explicit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from agora.commit_gate import CommitGate, GateContext
from agora.context_memory import ContextMemory, MemoryStateError, ViewAdmissionError
from agora.contracts import (
    REF_TARGETS,
    AgentIdentity,
    AssertionStatus,
    AudienceSnapshot,
    CapabilityEnvelope,
    CommitAuthorization,
    CommitDecision,
    Completeness,
    ComputeSink,
    ContentFragment,
    ContextSnapshot,
    DecisionKind,
    DeliveryReceipt,
    DeliveryState,
    Disposition,
    Effect,
    EffectKind,
    EffectPlan,
    EligibilityStatus,
    EnvelopePurpose,
    EnvironmentKind,
    FinishChoice,
    FragmentKind,
    HistoryVisibility,
    InstructionAuthority,
    Integrity,
    IntendedUse,
    Intent,
    IntentState,
    MainStepKind,
    MemoryAccessRequest,
    MemoryAccessResult,
    MemoryView,
    ModelCreateIntentStep,
    ModelFinishStep,
    ModelProposeMemoryStep,
    ModelRequestAccessStep,
    ModelSubmitPlanStep,
    OutcomeKind,
    Principal,
    PrincipalKind,
    Provenance,
    ReasonCode,
    Record,
    RecordRef,
    RecordStatus,
    RequestedPrecision,
    RuntimeOrigin,
    SinkKind,
    SinkStatus,
    SourceKind,
    TurnOutcome,
    ValidationError,
    Visibility,
    WorkItem,
    WorkState,
    canonical_digest,
    canonical_json_bytes,
    render_add_reaction,
    render_send_message,
)
from agora.fake_sidecar import FakeSidecar, SidecarError
from agora.ports import CrashCheckpoint, FaultInjector, MainModelPort, VirtualClock
from agora.stores import (
    CoreStore,
    IngestReceipt,
    InvalidReferenceError,
    RecordConflictError,
    TransportStore,
    ingest_batch,
)

MAX_MAIN_STEPS = 8
MAX_PLAN_SUBMISSIONS = 3
MAX_MESSAGE_CHARS = 2000

#: The purpose class the runtime mints for mechanical participation
#: intents. Always registered — the default vocabulary is never empty, and
#: section-less manifests still admit it — and a model CREATE_INTENT may
#: claim it like any other registered class.
MECHANICAL_PURPOSE_CLASS = "social-participation"


@dataclass(frozen=True, slots=True)
class TurnConfig:
    max_main_steps: int = MAX_MAIN_STEPS
    max_plan_submissions: int = MAX_PLAN_SUBMISSIONS
    max_message_chars: int = MAX_MESSAGE_CHARS
    #: The registered purpose-class vocabulary (handoff §4.4: bounded
    #: strings from the fixture registry, used to MATCH an existing
    #: permission, never sufficient evidence for issuing one). Runtime-
    #: wired from the manifest; a model CREATE_INTENT can only pick from
    #: it (bead njy).
    purpose_classes: frozenset[str] = frozenset({MECHANICAL_PURPOSE_CLASS})
    #: The fixed-time defer window the runtime mints for a FINISH(DEFER)
    #: resolution, which carries no times of its own (§6.4 fixed-time
    #: DEFER only): due at now + defer_window_ms, expired (abandoned
    #: without a visible effect) at due + defer_expiry_ms.
    defer_window_ms: int = 60_000
    defer_expiry_ms: int = 300_000


@dataclass(frozen=True, slots=True)
class TurnResult:
    outcome: TurnOutcome
    decision: CommitDecision | None
    receipt_ref: RecordRef | None


class BudgetExhausted(Exception):
    pass


class PlanRejected(Exception):
    """A draft fails a hard limit before plan construction."""

    def __init__(self, code: ReasonCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class Coordinator:
    def __init__(
        self,
        core: CoreStore,
        clock: VirtualClock,
        context_memory: ContextMemory,
        main_port: MainModelPort,
        gate: CommitGate,
        sidecar: FakeSidecar,
        *,
        identity: AgentIdentity,
        audience: AudienceSnapshot,
        capability: CapabilityEnvelope,
        sink_ref: RecordRef,
        config: TurnConfig | None = None,
        injector: FaultInjector | None = None,
    ) -> None:
        self.core = core
        self.clock = clock
        self.context_memory = context_memory
        self.main_port = main_port
        self.gate = gate
        self.sidecar = sidecar
        self.identity = identity
        self.audience = audience
        self.capability = capability
        self.sink_ref = sink_ref
        self.config = config or TurnConfig()
        self.injector = injector

    def _trip(self, checkpoint: CrashCheckpoint) -> None:
        if self.injector is not None:
            self.injector.trip(checkpoint)

    # -- identity bootstrap -------------------------------------------------

    @classmethod
    def bootstrap(
        cls,
        core: CoreStore,
        clock: VirtualClock,
        *,
        destination_id: str,
        allowed_destination_ids: tuple[str, ...],
    ) -> tuple[AgentIdentity, AudienceSnapshot, CapabilityEnvelope, RecordRef]:
        """Create the per-run fixture control records: exactly one agent
        identity, an audience snapshot for the destination, a standing
        capability envelope, and the scripted compute sink. Idempotent on a
        reopened store: an existing identity means the run's records are
        already in place, and they are read back instead of rewritten (a
        restart's advanced clock must not conflict with its own fixtures)."""
        existing = core.get_record("identity:agent")
        if isinstance(existing, AgentIdentity):
            audience = core.get_record(f"audience:{destination_id}")
            capability = core.get_record("capability:social")
            sink = core.get_record("sink:scripted")
            if audience is None or capability is None or sink is None:
                # An identity without its fixtures means the store was
                # bootstrapped for a different destination (or is torn):
                # fail loudly rather than silently return a stale capability
                # bound to another destination's envelope.
                raise ValueError(
                    f"identity:agent exists but the fixture records for"
                    f" destination {destination_id!r} do not"
                )
            assert isinstance(audience, AudienceSnapshot)
            assert isinstance(capability, CapabilityEnvelope)
            assert isinstance(sink, ComputeSink)
            return (
                existing,
                audience,
                capability,
                RecordRef(id=sink.id, revision=sink.revision),
            )
        now = clock.now_rfc3339()
        principal = Principal(
            id="principal:agent",
            revision=1,
            created_at=now,
            kind=PrincipalKind.AGENT,
            verified_bindings=(),
            claimed_aliases=("hermes",),
        )
        core.put_record(principal)
        identity = AgentIdentity(
            id="identity:agent",
            revision=1,
            created_at=now,
            principal_ref=RecordRef(id=principal.id, revision=principal.revision),
            preference_refs=(),
            autobiography_refs=(),
            commitment_refs=(),
            active_interest_refs=(),
        )
        core.put_record(identity)
        audience = AudienceSnapshot(
            id=f"audience:{destination_id}",
            revision=1,
            created_at=now,
            destination_id=destination_id,
            environment_kind=EnvironmentKind.ROOM,
            visibility=Visibility.PUBLIC,
            known_principal_refs=(RecordRef(id=principal.id, revision=principal.revision),),
            unknown_readers=True,
            history_visibility=HistoryVisibility.PUBLIC_HISTORY,
            completeness=Completeness.OBSERVED,
            disclosure_context_revision=1,
            valid_until=None,
        )
        core.put_record(audience)
        capability = CapabilityEnvelope(
            id="capability:social",
            revision=1,
            created_at=now,
            actor_ref=RecordRef(id=principal.id, revision=principal.revision),
            account_id="account:agent",
            allowed_destination_ids=allowed_destination_ids,
            allowed_effect_kinds=(EffectKind.SEND_MESSAGE, EffectKind.ADD_REACTION),
            provisioning_binding_refs=(),
            rate_budget_limits=(),
            valid_until=None,
            status=RecordStatus.ACTIVE,
        )
        core.put_record(capability)
        sink = ComputeSink(
            id="sink:scripted",
            revision=1,
            created_at=now,
            kind=SinkKind.SCRIPTED_TEST,
            configured_endpoint=None,
            permitted_data_classes=("public",),
            capability_ref=None,
            status=SinkStatus.ENABLED,
        )
        core.put_record(sink)
        return (
            identity,
            audience,
            capability,
            RecordRef(id=sink.id, revision=sink.revision),
        )

    # -- turn execution ------------------------------------------------------

    def ingest(
        self,
        transport: TransportStore,
        *,
        consumer_id: str,
        planner: Any = None,
        event_planner: Any = None,
    ) -> IngestReceipt:
        receipt = ingest_batch(
            self.core,
            transport,
            consumer_id=consumer_id,
            ingested_at=self.clock.now_rfc3339(),
            injector=self.injector,
            planner=planner,
            event_planner=event_planner,
        )
        return receipt

    def ready_work_ids(self) -> tuple[str, ...]:
        return self.core.ready_work_ids()

    def resume_work(self, work_id: str) -> None:
        """Resume a HELD work item on a named external change (§6.1:
        policy/resource/target/audience/review-service recovery, or an
        agent-chosen replacement plan within budgets): HOLD → READY with
        every counter and the dedup trigger key retained (§8:
        resumptions retain counters — never an automatic spin; the caller
        names the change)."""
        self.core.transition_work(work_id, WorkState.READY, expected_state=WorkState.HOLD)

    def run_turn(self, work_id: str, *, intent_ref: RecordRef | None = None) -> TurnResult:
        """Run one work item to a typed outcome. Steps are accepted with
        compare-and-set on the work counter, so a crash between model and
        acceptance re-accepts at most one continuation per step (§7).
        ``intent_ref`` is a fixture/test entry point for naming an
        already-minted intent; it must not be wired to model input — the
        model's step-level intent_ref is ignored, and every model-driven
        intent goes through _create_intent's registry check."""
        work = self.core.get_work(work_id)
        if work is None:
            raise ValueError(f"work {work_id} not found")
        if work["state"] != WorkState.READY.value:
            raise ValueError(f"work {work_id} is not READY")
        self.core.transition_work(work_id, WorkState.RUNNING, expected_state=WorkState.READY)
        with self.core.transaction() as connection:
            connection.execute(
                "UPDATE core_work SET lease_owner = 'agora:coordinator', lease_until_ms = ?"
                " WHERE work_id = ?",
                (self.clock.now_ms() + 2000, work_id),
            )

        chosen_intent = intent_ref or self._work_intent(work_id, work)
        chosen_intent = self._resume_deferred_intent(chosen_intent)
        self._ensure_work_item(work_id, work, chosen_intent)
        decision: CommitDecision | None = None
        receipt_ref: RecordRef | None = None
        outcome_kind: OutcomeKind | None = None
        reason_codes: list[ReasonCode] = []
        # Turn-held state (M2 5.1): views granted to this turn and still
        # usable, and typed notices the next model step sees in its envelope.
        held_view_refs: list[RecordRef] = []
        notices: list[str] = []
        episode_ref = None
        if work["episode_ref"] is not None:
            episode_ref = self.core.current_ref(work["episode_ref"])
        effect_refs: list[RecordRef] = []
        # Coordinator-minted terminal decisions commit with the outcome in
        # one transaction at turn end (yp): written mid-turn they would sit
        # in an un-checkpointed window where a crash orphans their fixed
        # ids, and the recovery turn's re-derivation conflicts against the
        # orphan across restarts.
        terminal_records: list[Record] = []

        try:
            for _ in range(self.config.max_main_steps):
                work_row = self.core.get_work(work_id)
                assert work_row is not None
                step_index = work_row["main_steps_used"]
                # Epoch-scoped call ids: a restart's replay generates fresh
                # records; the crashed run's unaccepted records remain as
                # history and never collide (§3, §7 CAS acceptance).
                call_id = f"{work_id}:{step_index}.e{self.core.runtime_epoch}"
                try:
                    snapshot = self.context_memory.build_snapshot(
                        identity_ref=RecordRef(
                            id=self.identity.id, revision=self.identity.revision
                        ),
                        intent_ref=chosen_intent,
                        audience_ref=RecordRef(
                            id=self.audience.id, revision=self.audience.revision
                        ),
                        episode_ref=episode_ref,
                        sink_ref=self.sink_ref,
                        effect_capable=True,
                        call_id=call_id,
                        active_view_refs=tuple(held_view_refs),
                    )
                except ViewAdmissionError as error:
                    # A turn-held view that can no longer be admitted (say,
                    # revoked or superseded between grant and use) is a typed
                    # denial: the turn holds with VIEW_FORBIDDEN and the work
                    # still completes — never a stranded RUNNING row with a
                    # live lease (review finding on run_turn exception safety).
                    decision = CommitDecision(
                        id=f"decision:admission:{work_id}:{step_index}",
                        revision=1,
                        created_at=self.clock.now_rfc3339(),
                        kind=DecisionKind.DENIED,
                        reason_codes=(error.code,),
                        authorization_ref=None,
                        assessment_ref=None,
                    )
                    terminal_records.append(decision)
                    outcome_kind = OutcomeKind.HELD
                    reason_codes = [error.code]
                    break
                messages = [{"role": "user", "content": _context_summary(snapshot)}]
                messages.extend({"role": "user", "content": notice} for notice in notices)
                envelope = self.context_memory.record_envelope(
                    snapshot=snapshot,
                    purpose=EnvelopePurpose.MAIN_STEP,
                    messages=messages,
                    sink_ref=self.sink_ref,
                    call_id=call_id,
                )
                try:
                    step = self.main_port.step(envelope)
                except RuntimeError:
                    # The port offers no further step (scripted exhaustion):
                    # silence is never inferred from a missing model (§6.4),
                    # so the standing outcome — a hold, if one stands — is
                    # the terminal state, not a fresh budget failure.
                    break
                self._trip(CrashCheckpoint.AFTER_MODEL_BEFORE_ACCEPT)

                # CAS: accept at most one continuation per step version.
                with self.core.transaction() as connection:
                    cursor = connection.execute(
                        "UPDATE core_work SET main_steps_used = main_steps_used + 1"
                        " WHERE work_id = ? AND main_steps_used = ?",
                        (work_id, step_index),
                    )
                    if cursor.rowcount != 1:
                        raise BudgetExhausted  # concurrent acceptance; not retried blindly
                self.context_memory.record_step_coverage(
                    snapshot=snapshot, envelope=envelope, call_id=call_id
                )

                if isinstance(step, ModelFinishStep):
                    if step.outcome == FinishChoice.DEFER:
                        # A DEFER resolution is a plan like any other (§6.1
                        # DRAFT→…→DEFERRED): it routes through the gate, so a
                        # strong-sensitive defer gets its applicable
                        # assessment instead of bypassing review (§6.4).
                        work_row = self.core.get_work(work_id)
                        assert work_row is not None
                        submissions = work_row["submissions_used"]
                        if submissions >= self.config.max_plan_submissions:
                            if outcome_kind is None:
                                outcome_kind = OutcomeKind.FAILED_INTERNAL
                                reason_codes.append(ReasonCode.BUDGET_EXHAUSTED)
                            break
                        # FINISH carries only the DEFER choice (§4.4), so the
                        # runtime mints the fixed-time window itself; a
                        # SUBMIT_PLAN(DEFER) supplies its own times.
                        defer_until = self.clock.at_rfc3339(
                            self.clock.now_ms() + self.config.defer_window_ms
                        )
                        defer_expires_at = self.clock.at_rfc3339(
                            self.clock.now_ms()
                            + self.config.defer_window_ms
                            + self.config.defer_expiry_ms
                        )
                        defer_step = ModelSubmitPlanStep(
                            kind=MainStepKind.SUBMIT_PLAN,
                            intent_ref=chosen_intent,
                            disposition=Disposition.DEFER,
                            effects=(),
                            declared_fact_refs=(),
                            defer_until=defer_until,
                            defer_expires_at=defer_expires_at,
                        )
                        decision, receipt_ref, refs = self._submit_plan(
                            work_id,
                            defer_step,
                            snapshot,
                            chosen_intent,
                            proposal_revision=submissions + 1,
                        )
                        effect_refs.extend(refs)
                        if decision.kind == DecisionKind.DEFERRED:
                            self._defer_intent(
                                work_id, chosen_intent, defer_until, defer_expires_at
                            )
                            outcome_kind = OutcomeKind.DEFERRED
                            reason_codes = []
                            break
                        # A held defer is terminal: the FINISH step signals
                        # completion intent, so a review-held deferral ends
                        # the turn HELD with the decision on record (a
                        # SUBMIT_PLAN(DEFER) step, by contrast, keeps the
                        # ordinary revision loop).
                        outcome_kind = OutcomeKind.HELD
                        reason_codes = list(decision.reason_codes)
                        notices.append(
                            "held:" + ",".join(code.value for code in decision.reason_codes)
                        )
                    else:
                        # An explicit stand-down after a held submission is
                        # normal silence: NO_ACTION, with the held decision
                        # left on record (the hold never auto-resolves).
                        outcome_kind = OutcomeKind.NO_ACTION
                        reason_codes = []
                    break
                if step.kind == MainStepKind.SUBMIT_PLAN:
                    work_row = self.core.get_work(work_id)
                    assert work_row is not None
                    submissions = work_row["submissions_used"]
                    if submissions >= self.config.max_plan_submissions:
                        if outcome_kind is None:
                            outcome_kind = OutcomeKind.FAILED_INTERNAL
                            reason_codes.append(ReasonCode.BUDGET_EXHAUSTED)
                        break
                    assert isinstance(step, ModelSubmitPlanStep)
                    decision, receipt_ref, refs = self._submit_plan(
                        work_id, step, snapshot, chosen_intent, proposal_revision=submissions + 1
                    )
                    effect_refs.extend(refs)
                    if decision.kind == DecisionKind.AUTHORIZED and receipt_ref is not None:
                        receipt_record = self.core.get_record(receipt_ref.id, receipt_ref.revision)
                        if (
                            isinstance(receipt_record, DeliveryReceipt)
                            and receipt_record.state == DeliveryState.UNKNOWN
                        ):
                            # the dispatch is in flight with uncertain remote
                            # visibility: never reported as committed, never
                            # blindly re-attempted (§6.2) — reconciliation
                            # owns the resolution
                            outcome_kind = OutcomeKind.DELIVERY_UNKNOWN
                            reason_codes = [ReasonCode.DELIVERY_UNKNOWN]
                        else:
                            outcome_kind = OutcomeKind.COMMITTED
                            reason_codes = []
                    elif decision.kind in (DecisionKind.DENIED, DecisionKind.HOLD):
                        # The turn holds, but the model may still offer a
                        # replacement within the submissions budget (§6.1
                        # bounded rewrite loop; never an automatic spin). A
                        # held required review stays local in the meantime:
                        # no dispatch, no fallback (I07/I13), and a rewrite
                        # is a fresh proposal revision.
                        outcome_kind = OutcomeKind.HELD
                        reason_codes = list(decision.reason_codes)
                        notices.append(
                            "held:" + ",".join(code.value for code in decision.reason_codes)
                        )
                    elif decision.kind == DecisionKind.QUIET_COMPLETED:
                        outcome_kind = OutcomeKind.NO_ACTION
                        reason_codes = []
                    elif decision.kind == DecisionKind.DEFERRED:
                        # a SUBMIT_PLAN(DEFER) resolution defers exactly like
                        # the FINISH(DEFER) path: the intent gets the model's
                        # supplied window and the turn ends DEFERRED (review
                        # finding: this branch was silently swallowed before)
                        assert step.defer_until is not None and step.defer_expires_at is not None
                        self._defer_intent(
                            work_id, chosen_intent, step.defer_until, step.defer_expires_at
                        )
                        outcome_kind = OutcomeKind.DEFERRED
                        reason_codes = []
                    if outcome_kind in (
                        OutcomeKind.COMMITTED,
                        OutcomeKind.NO_ACTION,
                        OutcomeKind.DEFERRED,
                        OutcomeKind.DELIVERY_UNKNOWN,
                    ):
                        break
                elif isinstance(step, ModelRequestAccessStep):
                    result = self._request_access(step, chosen_intent, snapshot, work_id, call_id)
                    notices.append(_access_notice(result))
                    if result.view_ref is not None:
                        view = self.core.get_record(result.view_ref.id, result.view_ref.revision)
                        if isinstance(view, MemoryView) and _OUTWARD_USES.intersection(
                            view.permitted_uses
                        ):
                            # Only outward-usable views are ever offered to a
                            # later snapshot: an internal-only view is never
                            # admissible to effect-capable context (M2 3.3),
                            # so it informs the turn as a typed notice only.
                            held_view_refs.append(result.view_ref)
                    continue
                elif isinstance(step, ModelProposeMemoryStep):
                    memory_ref = self._propose_memory(step, snapshot, work_id)
                    notices.append(f"memory:{memory_ref.id}:stored")
                    continue
                elif isinstance(step, ModelCreateIntentStep):
                    chosen_intent = self._create_intent(step.goal_text, step.purpose_class)
                    continue
                # PARTICIPATE / others: record and continue the loop.
                continue
            else:
                if outcome_kind is None:
                    outcome_kind = OutcomeKind.FAILED_INTERNAL
                    reason_codes.append(ReasonCode.BUDGET_EXHAUSTED)
        except BudgetExhausted:
            outcome_kind = OutcomeKind.FAILED_INTERNAL
            reason_codes.append(ReasonCode.BUDGET_EXHAUSTED)
        except PlanRejected as rejected:
            # A model-supplied reference or hard limit failed before plan
            # construction: a typed denial, never a crash (§5 coordinator
            # validates referenced ids).
            decision = CommitDecision(
                id=f"decision:reject:{work_id}",
                revision=1,
                created_at=self.clock.now_rfc3339(),
                kind=DecisionKind.DENIED,
                reason_codes=(rejected.code,),
                authorization_ref=None,
                assessment_ref=None,
            )
            terminal_records.append(decision)
            outcome_kind = OutcomeKind.HELD
            reason_codes.append(rejected.code)

        # A HELD turn parks the work in HOLD (§6.1: READY → RUNNING →
        # COMPLETED or HOLD — bead lcc): the hold stays resumable on a
        # named external change with counters retained, never
        # force-completed into an unresumable state. Every other outcome
        # completes. A resumed turn appends the next outcome revision —
        # the held turn's outcome stays as history.
        final_state = (
            WorkState.HOLD
            if (outcome_kind or OutcomeKind.FAILED_INTERNAL) == OutcomeKind.HELD
            else WorkState.COMPLETED
        )
        prior_outcome = self.core.current_ref(f"outcome:{work_id}")
        outcome = TurnOutcome(
            id=f"outcome:{work_id}",
            revision=(prior_outcome.revision + 1) if prior_outcome is not None else 1,
            created_at=self.clock.now_rfc3339(),
            kind=outcome_kind or OutcomeKind.FAILED_INTERNAL,
            intent_ref=chosen_intent,
            work_ref=RecordRef(id=work_id, revision=1),
            reason_codes=tuple(reason_codes),
            effect_refs=tuple(effect_refs),
        )
        # The terminal writes commit as one unit (yp): the coordinator's
        # terminal decisions, the fixed-id outcome record, and the work
        # completion UPDATE. A crash in the old two-transaction window
        # orphaned outcome:{work} and the recovery turn's rewrite of the
        # same id raised RecordConflictError on every restart — a crash
        # loop. Atomic here, a crash leaves the work cleanly un-terminated.
        with self.core.transaction() as connection:
            for record in terminal_records:
                self.core._put_record_tx(connection, record)
            self.core._put_record_tx(connection, outcome)
            self.core._verify_ref_targets_tx(connection, (*terminal_records, outcome))
            connection.execute(
                "UPDATE core_work SET terminal_outcome_ref = ?, state = ?,"
                " lease_owner = NULL, lease_until_ms = NULL WHERE work_id = ?",
                (outcome.id, final_state.value, work_id),
            )
            self._trip(CrashCheckpoint.DURING_COMPLETION_TX)
        audit_code = _audit_code(outcome)
        if audit_code is not None:
            self.core.audit(
                operation="turn",
                entity_ref=work_id,
                reason_code=audit_code,
                version_refs=(outcome.id,),
                at_ms=self.clock.now_ms(),
            )
        return TurnResult(outcome=outcome, decision=decision, receipt_ref=receipt_ref)

    # -- plan handling --------------------------------------------------------

    def _ensure_work_item(self, work_id: str, work: dict[str, Any], intent_ref: RecordRef) -> None:
        """Persist the §4.2 WorkItem record once per work item, at turn start
        under the resolved intent. Written lazily (mechanical work resolves
        its intent only here) and never rewritten — crash replay skips it,
        so the epoch-bearing bytes can never conflict with themselves."""
        if self.core.get_record(work_id) is not None:
            return
        episode_ref = None
        if work["episode_ref"] is not None:
            episode_ref = self.core.current_ref(work["episode_ref"])
            if episode_ref is None:
                raise ValueError(f"work {work_id} cites missing episode {work['episode_ref']}")
        item = WorkItem(
            id=work_id,
            revision=1,
            created_at=self.clock.now_rfc3339(),
            intent_ref=intent_ref,
            trigger_key=work["trigger_key"],
            episode_ref=episode_ref,
            state=WorkState.RUNNING,
            lease_owner="agora:coordinator",
            lease_until_ms=self.clock.now_ms() + 2000,
            runtime_epoch=self.core.runtime_epoch,
            main_steps_used=0,
            submissions_used=0,
            terminal_outcome_ref=None,
        )
        self.core.put_record(item)

    def _submit_plan(
        self,
        work_id: str,
        step: ModelSubmitPlanStep,
        snapshot: ContextSnapshot,
        intent_ref: RecordRef,
        *,
        proposal_revision: int,
    ) -> tuple[CommitDecision, RecordRef | None, list[RecordRef]]:
        work = self.core.get_work(work_id)
        assert work is not None
        self._validate_model_refs(step)
        if step.disposition == Disposition.DEFER:
            assert step.defer_until is not None and step.defer_expires_at is not None
            if self.clock.to_ms(step.defer_expires_at) < self.clock.to_ms(step.defer_until):
                # A defer whose expiry precedes its due can never wake: a
                # typed rejection, never a silently-dead deferral.
                raise PlanRejected(
                    ReasonCode.INVALID_SCHEMA, "defer_expires_at precedes defer_until"
                )
        # Build phase (pure): rendering and validation raise PlanRejected
        # before anything is written, so a bad effect can no longer strand
        # the writes of its earlier siblings.
        prepared: list[tuple[str, str, Effect]] = []
        for index, draft in enumerate(step.effects):
            rendered = self._render(draft)
            payload_id = f"payload:effect:{work_id}:{proposal_revision}:{index}"
            prepared.append(
                (
                    payload_id,
                    _json_dumps(rendered),
                    Effect(
                        id=f"{work_id}:effect:{proposal_revision}:{index}",
                        revision=1,
                        created_at=self.clock.now_rfc3339(),
                        logical_effect_id=f"{work_id}:effect:{index}",
                        kind=draft.kind,
                        account_id=self.capability.account_id,
                        destination_id=draft.destination_id,
                        target_ref=draft.target_ref,
                        rendered_payload_ref=payload_id,
                        payload_digest=canonical_digest(rendered),
                        capability_ref=RecordRef(
                            id=self.capability.id, revision=self.capability.revision
                        ),
                    ),
                )
            )
        effects = [effect for _, _, effect in prepared]
        plan = EffectPlan(
            id=f"plan:{work_id}:{proposal_revision}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            intent_ref=intent_ref,
            snapshot_ref=RecordRef(id=snapshot.id, revision=snapshot.revision),
            disposition=step.disposition,
            effects=tuple(effects),
            proposal_revision=proposal_revision,
            proposal_digest=canonical_digest(
                {
                    "disposition": step.disposition.value,
                    "effects": [_json_dumps(self._render(d)) for d in step.effects],
                    "intent": intent_ref.to_payload(),
                    # §4.4 exact-proposal binding: the plan's context is part
                    # of its identity, so a PASS keyed to the digest cannot
                    # silently travel to a plan built on another snapshot.
                    "snapshot": snapshot.id,
                    "coverage": snapshot.producer_coverage_ref.id,
                    # §4.4 binds the defer window too: a PASS keyed to a
                    # defer proposal cannot travel to the same defer with a
                    # different due/expiry.
                    "defer_until": step.defer_until,
                    "defer_expires_at": step.defer_expires_at,
                }
            ),
            defer_until=step.defer_until,
            defer_expires_at=step.defer_expires_at,
            declared_fact_refs=tuple(step.declared_fact_refs),
            observed_target_refs=(),
            producer_coverage_ref=RecordRef(
                id=snapshot.producer_coverage_ref.id,
                revision=snapshot.producer_coverage_ref.revision,
            ),
        )
        # Crash-atomic submit (yp): payloads, effects, the plan, and the
        # submissions counter commit as one unit. In the old per-write
        # transactions a crash mid-sequence left orphan effect/plan rows
        # beside an un-incremented counter, so the recovery turn's next
        # submission re-derived the same proposal_revision ids with a new
        # created_at and raised RecordConflictError on every restart — a
        # crash loop.
        with self.core.transaction() as connection:
            for payload_id, data, effect in prepared:
                self.core._put_payload_tx(connection, payload_id, data, owner_ref=f"work:{work_id}")
                self.core._put_record_tx(connection, effect)
            self.core._put_record_tx(connection, plan)
            self.core._verify_ref_targets_tx(connection, (*effects, plan))
            connection.execute(
                "UPDATE core_work SET submissions_used = submissions_used + 1 WHERE work_id = ?",
                (work_id,),
            )
            self._trip(CrashCheckpoint.DURING_SUBMIT_TX)
        decision = self.gate.evaluate(
            plan,
            context=GateContext(
                identity_ref=RecordRef(id=self.identity.id, revision=self.identity.revision),
                capability=self.capability,
                audience=self.audience,
                max_message_chars=self.config.max_message_chars,
            ),
        )
        receipt_ref: RecordRef | None = None
        refs = [RecordRef(id=effect.id, revision=effect.revision) for effect in effects]
        if decision.kind == DecisionKind.AUTHORIZED and effects:
            assert decision.authorization_ref is not None
            authorization = self.core.get_record(
                decision.authorization_ref.id, decision.authorization_ref.revision
            )
            assert isinstance(authorization, CommitAuthorization)
            try:
                receipt = self.sidecar.submit_authorized(
                    authorization,
                    effects[0],
                    rendered_payload=self._render(step.effects[0]),
                    snapshot_ref=RecordRef(id=snapshot.id, revision=snapshot.revision),
                )
            except SidecarError as error:
                # Dispatch-time prevention (M3 D1) is part of the commit
                # path: the outbox row is durably HELD with the typed code
                # and the turn surfaces it like any other hold — the
                # bounded rewrite loop may still offer a replacement, and
                # for a TOO_LATE supersession refusal the earlier
                # revision's receipt stands.
                decision = CommitDecision(
                    id=f"decision:dispatch:{work_id}:{proposal_revision}",
                    revision=1,
                    created_at=self.clock.now_rfc3339(),
                    kind=DecisionKind.HOLD,
                    reason_codes=(error.code,),
                    authorization_ref=decision.authorization_ref,
                    assessment_ref=None,
                )
                self.core.put_record(decision)
            else:
                receipt_ref = RecordRef(id=receipt.id, revision=receipt.revision)
        return decision, receipt_ref, refs

    def _validate_model_refs(self, step: ModelSubmitPlanStep) -> None:
        """Model-supplied references are verified before anything is written
        (handoff §5: the coordinator validates referenced IDs); a bad ref is
        a typed plan rejection, and the store's insert-time check remains
        the backstop for runtime-written records."""
        effect_targets = REF_TARGETS[Effect]["target_ref"]
        fact_targets = REF_TARGETS[EffectPlan]["declared_fact_refs"]
        checks: list[tuple[RecordRef, str | frozenset[str]]] = [
            *(
                (draft.target_ref, effect_targets)
                for draft in step.effects
                if draft.target_ref is not None
            ),
            *((ref, fact_targets) for ref in step.declared_fact_refs),
        ]
        for ref, allowed in checks:
            try:
                self.core.verify_reference(ref, allowed, label="model step")
            except InvalidReferenceError as error:
                raise PlanRejected(ReasonCode.INVALID_REFERENCE, str(error)) from error

    def _request_access(
        self,
        step: ModelRequestAccessStep,
        intent_ref: RecordRef,
        snapshot: ContextSnapshot,
        work_id: str,
        call_id: str,
    ) -> MemoryAccessResult:
        """Bind a model access request to the authenticated runtime (handoff
        §5): intent, audience, and runtime origin are runtime-supplied — the
        model selects only resource, precision, use, and necessity. Malformed
        payloads and bad references are typed rejections, never crashes, and
        no field the model supplies can widen the request's bindings."""
        request = step.request
        model_id = request.get("request_id")
        if not isinstance(model_id, str) or not model_id:
            model_id = "access"
        resource_id = request.get("resource_id")
        if not isinstance(resource_id, str | None):
            raise PlanRejected(ReasonCode.INVALID_SCHEMA, "resource_id must be a string")
        if resource_id is not None and not isinstance(
            self.core.get_record(resource_id), ContentFragment
        ):
            raise PlanRejected(
                ReasonCode.INVALID_REFERENCE, f"resource {resource_id!r} does not resolve"
            )
        data_class = request.get("data_class")
        if (resource_id is None) == (data_class is None):
            raise PlanRejected(
                ReasonCode.INVALID_SCHEMA,
                "exactly one of resource_id or data_class is required",
            )
        audience_payload = request.get("audience_ref")
        if audience_payload is not None:
            parsed = _parse_ref(audience_payload)
            if parsed is None or parsed.id != self.audience.id:
                raise PlanRejected(
                    ReasonCode.CONTEXT_MISMATCH,
                    "an access request is bound to the turn's audience",
                )
        trigger_refs: list[RecordRef] = []
        for payload in request.get("trigger_refs", ()):
            ref = _parse_ref(payload)
            if ref is None:
                raise PlanRejected(ReasonCode.INVALID_REFERENCE, "malformed trigger ref")
            try:
                self.core.verify_reference(
                    ref, REF_TARGETS[MemoryAccessRequest]["trigger_refs"], label="model step"
                )
            except InvalidReferenceError as error:
                raise PlanRejected(ReasonCode.INVALID_REFERENCE, str(error)) from error
            trigger_refs.append(ref)
        try:
            intended_use = IntendedUse(request["intended_use"])
            precision = RequestedPrecision(request["requested_precision"])
        except (KeyError, ValueError) as error:
            raise PlanRejected(
                ReasonCode.INVALID_SCHEMA, f"malformed access request: {error}"
            ) from error
        requested_fields = request.get("requested_fields", ())
        if not isinstance(requested_fields, (list, tuple)) or not all(
            isinstance(field, str) for field in requested_fields
        ):
            raise PlanRejected(ReasonCode.INVALID_SCHEMA, "requested_fields must be strings")
        transform_id = request.get("transform_id")
        if transform_id is not None and not isinstance(transform_id, str):
            raise PlanRejected(ReasonCode.INVALID_SCHEMA, "transform_id must be a string")
        necessity_text = request.get("necessity_text")
        if necessity_text is not None and not isinstance(necessity_text, str):
            raise PlanRejected(ReasonCode.INVALID_SCHEMA, "necessity_text must be a string")
        try:
            bound = MemoryAccessRequest(
                id=f"access:{call_id}:{model_id}",
                revision=1,
                created_at=self.clock.now_rfc3339(),
                request_id=f"{call_id}:{model_id}",
                intent_ref=intent_ref,
                resource_id=resource_id,
                data_class=data_class,
                requested_precision=precision,
                requested_fields=tuple(requested_fields),
                transform_id=transform_id,
                audience_ref=RecordRef(id=self.audience.id, revision=self.audience.revision),
                intended_use=intended_use,
                necessity_text=necessity_text,
                necessity_payload_ref=None,
                trigger_refs=tuple(trigger_refs),
                runtime_origin=self._runtime_origin(snapshot, work_id),
            )
        except ValidationError as error:
            # model-supplied field values that pass the shape checks above
            # can still fail the record contract's own rules — a typed
            # rejection, never a crash out of the turn (review finding)
            raise PlanRejected(ReasonCode.INVALID_SCHEMA, str(error)) from error
        return self.context_memory.request_access(bound)

    def _propose_memory(
        self, step: ModelProposeMemoryStep, snapshot: ContextSnapshot, work_id: str
    ) -> RecordRef:
        """PROPOSE_MEMORY with runtime-origin binding: lineage verification
        against the turn's actual coverage happens in ContextMemory; a typed
        memory-state failure (forged lineage, scope widening, skipped stage)
        rejects the turn, never a partial write."""
        try:
            return self.context_memory.propose_memory(
                assertion_text=step.assertion_text,
                source_refs=tuple(step.source_refs),
                requested_stage=step.requested_stage,
                requested_scope_kind=step.requested_scope_kind,
                scope_ref=step.scope_ref,
                existing_memory_ref=step.existing_memory_ref,
                runtime_origin=self._runtime_origin(snapshot, work_id),
            )
        except MemoryStateError as error:
            raise PlanRejected(error.code, str(error)) from error

    def _runtime_origin(self, snapshot: ContextSnapshot, work_id: str) -> RuntimeOrigin:
        """The coordinator-minted origin every memory-bound record carries:
        actor, work, snapshot, coverage, and epoch — none of it model-set."""
        return RuntimeOrigin(
            actor_ref=RecordRef(
                id=self.identity.principal_ref.id, revision=self.identity.principal_ref.revision
            ),
            work_ref=RecordRef(id=work_id, revision=1),
            snapshot_ref=RecordRef(id=snapshot.id, revision=snapshot.revision),
            trigger_event_refs=(),
            producer_coverage_ref=RecordRef(
                id=snapshot.producer_coverage_ref.id,
                revision=snapshot.producer_coverage_ref.revision,
            ),
            runtime_epoch=self.core.runtime_epoch,
        )

    def _render(self, draft: Any) -> dict[str, Any]:
        if draft.kind == EffectKind.SEND_MESSAGE:
            if draft.text is not None and len(draft.text) > self.config.max_message_chars:
                raise PlanRejected(ReasonCode.POLICY_FORBIDDEN, "message too long")
            return render_send_message(
                destination_id=draft.destination_id, text=draft.text or "", reply_target_ref=None
            )
        assert draft.target_ref is not None
        return render_add_reaction(
            destination_id=draft.destination_id,
            target_ref=draft.target_ref,
            target_content_digest=self._target_content_digest(draft.target_ref),
            emoji=draft.emoji or "🙂",
        )

    def _target_content_digest(self, target_ref: RecordRef) -> str:
        """Content digest of the reaction target (handoff §4.3: a reaction
        requires target message revision/content digest) — the target
        fragment's own payload bytes at the cited revision, so the rendered
        bytes bind exactly the content being reacted to and dispatch-time
        TARGET_CHANGED revalidation compares what was rendered. A missing
        fragment or a purged payload is a typed rejection, never a
        placeholder digest."""
        fragment = self.core.get_record(target_ref.id, target_ref.revision)
        if not isinstance(fragment, ContentFragment):
            raise PlanRejected(
                ReasonCode.INVALID_REFERENCE, "reaction target is not a content fragment"
            )
        data = self.core.get_payload(fragment.payload_ref)
        if data is None:
            raise PlanRejected(
                ReasonCode.INVALID_REFERENCE, "reaction target payload is missing or purged"
            )
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def _defer_intent(
        self, work_id: str, intent_ref: RecordRef, defer_until: str, defer_expires_at: str
    ) -> None:
        """Persist a deferred intent with its explicit fixed-time window
        (§6.1: DEFER requires an explicit due time and expiry; never a
        promise to reply). The DEFERRED decision itself comes from the
        gate, which routed the defer plan."""
        intent_record = self.core.get_record(intent_ref.id, intent_ref.revision)
        if isinstance(intent_record, Intent):
            deferred = _intent_revision(
                intent_record,
                state=IntentState.DEFERRED,
                due_at=defer_until,
                expires_at=defer_expires_at,
            )
            self.core.put_record(deferred)

    def _resume_deferred_intent(self, intent_ref: RecordRef) -> RecordRef:
        """The DEFERRED → OPEN transition (§6.1: exactly one deduped timer
        occurrence resumes the intent). Any turn that adopts a DEFERRED
        intent — the timer work row bound to it, or a fixture-named ref —
        resumes it first, so the wake runs under the deferred goal's own
        commitments instead of a fresh mechanical intent (bead 9xk). The
        consumed defer window is cleared; idempotent by record CAS: a
        crash after the bump re-derives an OPEN intent."""
        record = self.core.get_record(intent_ref.id)
        if not isinstance(record, Intent) or record.state != IntentState.DEFERRED:
            return intent_ref
        resumed = _intent_revision(record, state=IntentState.OPEN, clear_window=True)
        self.core.put_record(resumed)
        return RecordRef(id=resumed.id, revision=resumed.revision)

    def abandon_intent(self, intent_ref: RecordRef) -> RecordRef:
        """Expiry abandons a DEFERRED intent without a visible effect
        (§6.4): one revision bump to ABANDONED, no work, no outcome. Any
        other source state is a state-machine violation, not an expiry."""
        record = self.core.get_record(intent_ref.id)
        if not isinstance(record, Intent):
            raise ValueError(f"intent {intent_ref.id} not found")
        if record.state != IntentState.DEFERRED:
            raise ValueError(
                f"intent {intent_ref.id} is {record.state.value}, not DEFERRED:"
                " only an expiry abandons"
            )
        abandoned = _intent_revision(record, state=IntentState.ABANDONED, clear_window=True)
        self.core.put_record(abandoned)
        return RecordRef(id=abandoned.id, revision=abandoned.revision)

    def _create_intent(self, goal_text: str, purpose_class: str) -> RecordRef:
        """Grantless autonomous intent creation (I01/D02): a valid goal from
        the authenticated agent creates an intent; no grant prerequisite.
        The purpose class must be REGISTERED (handoff §4.4: bounded strings
        from the fixture registry, used to match an existing permission,
        never sufficient evidence for issuing one) — a model-invented class
        is a typed hold, so no model-supplied string reaches authority
        evaluation (bead njy). The stem is the goal digest, uniquified per
        creation — the same goal text may ground more than one intent over
        time — and a pathological id collision is a typed CONFLICT, never a
        crash."""
        if purpose_class not in self.config.purpose_classes:
            raise PlanRejected(
                ReasonCode.POLICY_FORBIDDEN,
                f"purpose class {purpose_class!r} is not in the registered vocabulary",
            )
        stem = canonical_digest(goal_text)[:20]
        suffix = 2
        while self.core.get_record(f"intent:agent:{stem}") is not None:
            stem = f"{canonical_digest(goal_text)[:20]}:{suffix}"
            suffix += 1
        payload_id = f"payload:goal:{stem}"
        fragment = ContentFragment(
            id=f"fragment:goal:{stem}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            kind=FragmentKind.TEXT,
            payload_ref=payload_id,
            provenance=Provenance(
                producer_ref=RecordRef(id=self.identity.principal_ref.id, revision=1),
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
                occurred_at=self.clock.now_rfc3339(),
                observed_at=self.clock.now_rfc3339(),
            ),
            commitment_refs=(),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
        intent = Intent(
            id=f"intent:agent:{stem}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            creator_ref=RecordRef(id=self.identity.principal_ref.id, revision=1),
            goal_fragment_ref=RecordRef(id=fragment.id, revision=fragment.revision),
            purpose_class=purpose_class,
            observed_trigger_refs=(),
            endorsement_revision=1,
            producer_coverage_ref=None,
            runtime_task_root_ids=(f"root:{canonical_digest(goal_text)[:12]}",),
            parent_work_ref=None,
            allowed_destination_refs=self.capability.allowed_destination_ids,
            due_at=None,
            expires_at=None,
            state=IntentState.OPEN,
        )
        # Crash-atomic mint (yp): same one-transaction shape as the
        # mechanical intent — no orphaned fixed-id fragment behind a lost
        # intent.
        try:
            with self.core.transaction() as connection:
                self.core._put_payload_tx(
                    connection, payload_id, goal_text, owner_ref="identity:agent"
                )
                self.core._put_record_tx(connection, fragment)
                self.core._put_record_tx(connection, intent)
                self.core._verify_ref_targets_tx(connection, (fragment, intent))
                self._trip(CrashCheckpoint.DURING_INTENT_MINT)
        except RecordConflictError as error:
            raise PlanRejected(ReasonCode.CONFLICT, str(error)) from error
        return RecordRef(id=intent.id, revision=intent.revision)

    def _work_intent(self, work_id: str, work: dict[str, Any]) -> RecordRef:
        """Every turn runs under an intent: the work row's own intent_ref
        when it names one (a timer wake bound to the deferred intent it
        resumes, bead 9xk), else a mechanical participation intent minted
        here (no grant involved)."""
        intent_id = work["intent_ref"] or f"intent:turn:{work_id}"
        existing = self.core.get_record(intent_id)
        if existing is not None:
            return RecordRef(id=existing.id, revision=existing.revision)
        payload_id = f"payload:goal:turn:{canonical_digest(work_id)[:16]}"
        fragment = ContentFragment(
            id=f"fragment:goal:turn:{canonical_digest(work_id)[:16]}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            kind=FragmentKind.TEXT,
            payload_ref=payload_id,
            provenance=Provenance(
                producer_ref=RecordRef(id=self.identity.principal_ref.id, revision=1),
                source_kind=SourceKind.AGENT_OUTPUT,
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
                occurred_at=self.clock.now_rfc3339(),
                observed_at=self.clock.now_rfc3339(),
            ),
            commitment_refs=(),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
        intent = Intent(
            id=intent_id,
            revision=1,
            created_at=self.clock.now_rfc3339(),
            creator_ref=RecordRef(id=self.identity.principal_ref.id, revision=1),
            goal_fragment_ref=RecordRef(id=fragment.id, revision=fragment.revision),
            purpose_class=MECHANICAL_PURPOSE_CLASS,
            observed_trigger_refs=(),
            endorsement_revision=1,
            producer_coverage_ref=None,
            runtime_task_root_ids=(f"root:turn:{canonical_digest(work_id)[:12]}",),
            parent_work_ref=None,
            allowed_destination_refs=self.capability.allowed_destination_ids,
            due_at=None,
            expires_at=None,
            state=IntentState.OPEN,
        )
        # Crash-atomic mint (yp): payload, fragment, and intent commit as
        # one unit — a crash between the old per-write commits left the
        # fixed-id fragment behind without its intent, and the recovery
        # re-mint surfaced that as a spurious CONFLICT hold.
        try:
            with self.core.transaction() as connection:
                self.core._put_payload_tx(
                    connection,
                    payload_id,
                    f"participation for {work['trigger_key']}",
                    owner_ref="identity:agent",
                )
                self.core._put_record_tx(connection, fragment)
                self.core._put_record_tx(connection, intent)
                self.core._verify_ref_targets_tx(connection, (fragment, intent))
                self._trip(CrashCheckpoint.DURING_INTENT_MINT)
        except RecordConflictError as error:
            raise PlanRejected(ReasonCode.CONFLICT, str(error)) from error
        return RecordRef(id=intent.id, revision=intent.revision)


_OUTCOME_AUDIT_CODES: dict[OutcomeKind, ReasonCode] = {
    OutcomeKind.COMMITTED: ReasonCode.COMMITTED,
    OutcomeKind.NO_ACTION: ReasonCode.NO_ACTION,
    OutcomeKind.DEFERRED: ReasonCode.DEFERRED,
    OutcomeKind.DELIVERY_UNKNOWN: ReasonCode.DELIVERY_UNKNOWN,
    OutcomeKind.HELD: ReasonCode.REVIEW_CONCERN,
}


def _intent_revision(
    record: Intent,
    *,
    state: IntentState,
    due_at: str | None = None,
    expires_at: str | None = None,
    clear_window: bool = False,
) -> Intent:
    """One intent state transition as a new revision: every other field
    carries over unchanged (handoff §6.1 intent state machine). A consumed
    defer window (a resume or an expiry) is cleared rather than left as a
    stale due that a future due-keyed query could resurrect."""
    return Intent(
        id=record.id,
        revision=record.revision + 1,
        created_at=record.created_at,
        creator_ref=record.creator_ref,
        goal_fragment_ref=record.goal_fragment_ref,
        purpose_class=record.purpose_class,
        observed_trigger_refs=record.observed_trigger_refs,
        endorsement_revision=record.endorsement_revision,
        producer_coverage_ref=record.producer_coverage_ref,
        runtime_task_root_ids=record.runtime_task_root_ids,
        parent_work_ref=record.parent_work_ref,
        allowed_destination_refs=record.allowed_destination_refs,
        due_at=None if clear_window else (record.due_at if due_at is None else due_at),
        expires_at=None
        if clear_window
        else (record.expires_at if expires_at is None else expires_at),
        state=state,
    )


def _audit_code(outcome: TurnOutcome) -> str | None:
    """The outcome's own reason code when it has one, else the kind's
    standing code. FAILED_INTERNAL intentionally has no standing code: a
    reasonless internal failure is not budget exhaustion, and an audit row
    without an honest code is worse than no row."""
    if outcome.reason_codes:
        return outcome.reason_codes[0].value
    mapped = _OUTCOME_AUDIT_CODES.get(outcome.kind)
    return None if mapped is None else mapped.value


def _json_dumps(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _context_summary(snapshot: ContextSnapshot) -> str:
    return f"snapshot:{snapshot.id}:fragments:{len(snapshot.selected_fragment_refs)}"


#: Permitted uses that can justify a view in effect-capable context;
#: INTERNAL_ONLY views stay out of snapshots entirely (M2 3.3).
_OUTWARD_USES = frozenset({IntendedUse.EXPRESSION, IntendedUse.OBSERVABLE_INFLUENCE})


def _access_notice(result: MemoryAccessResult) -> str:
    """The typed access outcome the model sees in its next envelope: status,
    view id, permitted uses, reason codes — never payload bytes (an
    internal-only view informs the turn as this summary alone)."""
    view_id = result.view_ref.id if result.view_ref is not None else "-"
    uses = ",".join(use.value for use in result.permitted_uses) or "-"
    codes = ",".join(code.value for code in result.reason_codes) or "-"
    return f"access:{result.access_status.value}:view:{view_id}:uses:{uses}:codes:{codes}"


def _parse_ref(payload: Any) -> RecordRef | None:
    if isinstance(payload, RecordRef):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("id"), str):
        revision = payload.get("revision", 1)
        if isinstance(revision, int) and revision >= 1:
            return RecordRef(id=payload["id"], revision=revision)
    return None
