"""Coordinator for the M1 slice (handoff §2, §5, §6.1).

Owns event ingestion orchestration, work episodes, main steps with budgets,
and outcome persistence. The coordinator supplies every verified actor,
coverage, and authority field — the model supplies only model-selectable
content. No grant is created from model text; no sidecar table is written
directly. Scripted participation makes silence explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agora.commit_gate import CommitGate, GateContext
from agora.context_memory import ContextMemory
from agora.contracts import (
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
    Intent,
    IntentState,
    MainStepKind,
    ModelCreateIntentStep,
    ModelFinishStep,
    ModelSubmitPlanStep,
    OutcomeKind,
    Principal,
    PrincipalKind,
    Provenance,
    ReasonCode,
    RecordRef,
    RecordStatus,
    SinkKind,
    SinkStatus,
    SourceKind,
    TurnOutcome,
    Visibility,
    WorkState,
    canonical_digest,
    canonical_json_bytes,
    render_add_reaction,
    render_send_message,
)
from agora.fake_sidecar import FakeSidecar
from agora.ports import CrashCheckpoint, FaultInjector, MainModelPort, VirtualClock
from agora.stores import CoreStore, IngestReceipt, TransportStore, ingest_batch

MAX_MAIN_STEPS = 8
MAX_PLAN_SUBMISSIONS = 3
MAX_MESSAGE_CHARS = 2000


@dataclass(frozen=True, slots=True)
class TurnConfig:
    max_main_steps: int = MAX_MAIN_STEPS
    max_plan_submissions: int = MAX_PLAN_SUBMISSIONS
    max_message_chars: int = MAX_MESSAGE_CHARS


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
        capability envelope, and the scripted compute sink."""
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

    def ingest(self, transport: TransportStore, *, consumer_id: str) -> IngestReceipt:
        receipt = ingest_batch(
            self.core,
            transport,
            consumer_id=consumer_id,
            ingested_at=self.clock.now_rfc3339(),
            injector=self.injector,
        )
        return receipt

    def ready_work_ids(self) -> tuple[str, ...]:
        return self.core.ready_work_ids()

    def run_turn(self, work_id: str, *, intent_ref: RecordRef | None = None) -> TurnResult:
        """Run one work item to a typed outcome. Steps are accepted with
        compare-and-set on the work counter, so a crash between model and
        acceptance re-accepts at most one continuation per step (§7)."""
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
        decision: CommitDecision | None = None
        receipt_ref: RecordRef | None = None
        outcome_kind: OutcomeKind | None = None
        reason_codes: list[ReasonCode] = []
        effect_refs: list[RecordRef] = []

        try:
            for _ in range(self.config.max_main_steps):
                work_row = self.core.get_work(work_id)
                assert work_row is not None
                step_index = work_row["main_steps_used"]
                # Epoch-scoped call ids: a restart's replay generates fresh
                # records; the crashed run's unaccepted records remain as
                # history and never collide (§3, §7 CAS acceptance).
                call_id = f"{work_id}:{step_index}.e{self.core.runtime_epoch}"
                snapshot = self.context_memory.build_snapshot(
                    identity_ref=RecordRef(id=self.identity.id, revision=self.identity.revision),
                    intent_ref=chosen_intent,
                    audience_ref=RecordRef(id=self.audience.id, revision=self.audience.revision),
                    episode_ref=None,
                    sink_ref=self.sink_ref,
                    effect_capable=True,
                    call_id=call_id,
                )
                envelope = self.context_memory.record_envelope(
                    snapshot=snapshot,
                    purpose=EnvelopePurpose.MAIN_STEP,
                    messages=[{"role": "user", "content": _context_summary(snapshot)}],
                    sink_ref=self.sink_ref,
                    call_id=call_id,
                )
                step = self.main_port.step(envelope)
                if self.injector is not None:
                    self.injector.trip(CrashCheckpoint.AFTER_MODEL_BEFORE_ACCEPT)

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
                        decision, receipt_ref, refs = self._defer(work_id, chosen_intent)
                        effect_refs.extend(refs)
                        outcome_kind = OutcomeKind.DEFERRED
                    else:
                        outcome_kind = OutcomeKind.NO_ACTION
                        reason_codes.append(ReasonCode.NO_ACTION)
                    break
                if step.kind == MainStepKind.SUBMIT_PLAN:
                    work_row = self.core.get_work(work_id)
                    assert work_row is not None
                    submissions = work_row["submissions_used"]
                    if submissions >= self.config.max_plan_submissions:
                        outcome_kind = OutcomeKind.FAILED_INTERNAL
                        reason_codes.append(ReasonCode.BUDGET_EXHAUSTED)
                        break
                    assert isinstance(step, ModelSubmitPlanStep)
                    decision, receipt_ref, refs = self._submit_plan(
                        work_id, step, snapshot, chosen_intent, proposal_revision=submissions + 1
                    )
                    effect_refs.extend(refs)
                    if decision.kind == DecisionKind.AUTHORIZED and receipt_ref is not None:
                        outcome_kind = OutcomeKind.COMMITTED
                    elif decision.kind == DecisionKind.DENIED:
                        outcome_kind = OutcomeKind.HELD
                        reason_codes.extend(decision.reason_codes)
                    elif decision.kind == DecisionKind.QUIET_COMPLETED:
                        outcome_kind = OutcomeKind.NO_ACTION
                    break
                if isinstance(step, ModelCreateIntentStep):
                    chosen_intent = self._create_intent(step.goal_text, step.purpose_class)
                    continue
                # PARTICIPATE / others: record and continue the loop.
                continue
            else:
                outcome_kind = OutcomeKind.FAILED_INTERNAL
                reason_codes.append(ReasonCode.BUDGET_EXHAUSTED)
        except BudgetExhausted:
            outcome_kind = OutcomeKind.FAILED_INTERNAL
            reason_codes.append(ReasonCode.BUDGET_EXHAUSTED)

        outcome = TurnOutcome(
            id=f"outcome:{work_id}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            kind=outcome_kind or OutcomeKind.FAILED_INTERNAL,
            intent_ref=chosen_intent,
            work_ref=RecordRef(id=work_id, revision=1),
            reason_codes=tuple(reason_codes),
            effect_refs=tuple(effect_refs),
        )
        self.core.put_record(outcome)
        with self.core.transaction() as connection:
            connection.execute(
                "UPDATE core_work SET terminal_outcome_ref = ?, state = ? WHERE work_id = ?",
                (outcome.id, WorkState.COMPLETED.value, work_id),
            )
        audit_code = _audit_code(outcome)
        self.core.audit(
            operation="turn",
            entity_ref=work_id,
            reason_code=audit_code,
            version_refs=(outcome.id,),
            at_ms=self.clock.now_ms(),
        )
        return TurnResult(outcome=outcome, decision=decision, receipt_ref=receipt_ref)

    # -- plan handling --------------------------------------------------------

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
        effects: list[Effect] = []
        for index, draft in enumerate(step.effects):
            rendered = self._render(draft)
            payload_id = f"payload:effect:{work_id}:{proposal_revision}:{index}"
            self.core.put_payload(
                payload_id,
                _json_dumps(rendered),
                owner_ref=f"work:{work_id}",
            )
            effect = Effect(
                id=f"{work_id}:effect:{index}",
                revision=1,
                created_at=self.clock.now_rfc3339(),
                logical_effect_id=f"{work_id}:effect:{index}",
                kind=draft.kind,
                account_id=self.capability.account_id,
                destination_id=draft.destination_id,
                target_ref=draft.target_ref,
                rendered_payload_ref=payload_id,
                payload_digest=canonical_digest(rendered),
                capability_ref=RecordRef(id=self.capability.id, revision=self.capability.revision),
            )
            self.core.put_record(effect)
            effects.append(effect)
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
                }
            ),
            defer_until=None,
            defer_expires_at=None,
            declared_fact_refs=tuple(step.declared_fact_refs),
            observed_target_refs=(),
            producer_coverage_ref=RecordRef(
                id=snapshot.producer_coverage_ref.id,
                revision=snapshot.producer_coverage_ref.revision,
            ),
        )
        self.core.put_record(plan)
        with self.core.transaction() as connection:
            connection.execute(
                "UPDATE core_work SET submissions_used = submissions_used + 1 WHERE work_id = ?",
                (work_id,),
            )
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
        refs: list[RecordRef] = [RecordRef(id=plan.id, revision=plan.revision)]
        if decision.kind == DecisionKind.AUTHORIZED and effects:
            assert decision.authorization_ref is not None
            authorization = self.core.get_record(
                decision.authorization_ref.id, decision.authorization_ref.revision
            )
            assert isinstance(authorization, CommitAuthorization)
            receipt = self.sidecar.submit_authorized(
                authorization, effects[0], rendered_payload=self._render(step.effects[0])
            )
            receipt_ref = RecordRef(id=receipt.id, revision=receipt.revision)
            refs.append(receipt_ref)
            refs.extend(RecordRef(id=effect.id, revision=effect.revision) for effect in effects)
        return decision, receipt_ref, refs

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
            target_content_digest="0" * 64,
            emoji=draft.emoji or "🙂",
        )

    def _defer(
        self, work_id: str, intent_ref: RecordRef
    ) -> tuple[CommitDecision, RecordRef | None, list[RecordRef]]:
        """FINISH/DEFER: persist a deferred intent with an explicit due
        condition (fixed time in this slice) — never a promise to reply."""
        intent_record = self.core.get_record(intent_ref.id, intent_ref.revision)
        if isinstance(intent_record, Intent):
            deferred = Intent(
                id=intent_record.id,
                revision=intent_record.revision + 1,
                created_at=intent_record.created_at,
                creator_ref=intent_record.creator_ref,
                goal_fragment_ref=intent_record.goal_fragment_ref,
                purpose_class=intent_record.purpose_class,
                observed_trigger_refs=intent_record.observed_trigger_refs,
                endorsement_revision=intent_record.endorsement_revision,
                producer_coverage_ref=intent_record.producer_coverage_ref,
                runtime_task_root_ids=intent_record.runtime_task_root_ids,
                parent_work_ref=intent_record.parent_work_ref,
                allowed_destination_refs=intent_record.allowed_destination_refs,
                due_at=self.clock.now_rfc3339(),
                expires_at=None,
                state=IntentState.DEFERRED,
            )
            self.core.put_record(deferred)
        decision = CommitDecision(
            id=f"decision:defer:{work_id}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            kind=DecisionKind.DEFERRED,
            reason_codes=(ReasonCode.DEFERRED,),
            authorization_ref=None,
            assessment_ref=None,
        )
        self.core.put_record(decision)
        return decision, None, []

    def _create_intent(self, goal_text: str, purpose_class: str) -> RecordRef:
        """Grantless autonomous intent creation (I01/D02): a valid goal from
        the authenticated agent creates an intent; no grant prerequisite."""
        payload_id = f"payload:goal:{canonical_digest(goal_text)[:20]}"
        self.core.put_payload(payload_id, goal_text, owner_ref="identity:agent")
        fragment = ContentFragment(
            id=f"fragment:goal:{canonical_digest(goal_text)[:20]}",
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
        self.core.put_record(fragment)
        intent = Intent(
            id=f"intent:agent:{canonical_digest(goal_text)[:20]}",
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
        self.core.put_record(intent)
        return RecordRef(id=intent.id, revision=intent.revision)

    def _work_intent(self, work_id: str, work: dict[str, Any]) -> RecordRef:
        """Every turn runs under an intent; turns without a fixture intent
        get a mechanical participation intent (no grant involved)."""
        intent_id = f"intent:turn:{work_id}"
        existing = self.core.get_record(intent_id)
        if existing is not None:
            return RecordRef(id=existing.id, revision=existing.revision)
        payload_id = f"payload:goal:turn:{canonical_digest(work_id)[:16]}"
        self.core.put_payload(
            payload_id, f"participation for {work['trigger_key']}", owner_ref="identity:agent"
        )
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
        self.core.put_record(fragment)
        intent = Intent(
            id=intent_id,
            revision=1,
            created_at=self.clock.now_rfc3339(),
            creator_ref=RecordRef(id=self.identity.principal_ref.id, revision=1),
            goal_fragment_ref=RecordRef(id=fragment.id, revision=fragment.revision),
            purpose_class="social-participation",
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
        self.core.put_record(intent)
        return RecordRef(id=intent.id, revision=intent.revision)


_OUTCOME_AUDIT_CODES: dict[OutcomeKind, ReasonCode] = {
    OutcomeKind.COMMITTED: ReasonCode.COMMITTED,
    OutcomeKind.NO_ACTION: ReasonCode.NO_ACTION,
    OutcomeKind.DEFERRED: ReasonCode.DEFERRED,
    OutcomeKind.DELIVERY_UNKNOWN: ReasonCode.DELIVERY_UNKNOWN,
    OutcomeKind.FAILED_INTERNAL: ReasonCode.BUDGET_EXHAUSTED,
    OutcomeKind.HELD: ReasonCode.REVIEW_CONCERN,
}


def _audit_code(outcome: TurnOutcome) -> str:
    if outcome.reason_codes:
        return outcome.reason_codes[0].value
    return _OUTCOME_AUDIT_CODES[outcome.kind].value


def _json_dumps(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _context_summary(snapshot: ContextSnapshot) -> str:
    return f"snapshot:{snapshot.id}:fragments:{len(snapshot.selected_fragment_refs)}"
