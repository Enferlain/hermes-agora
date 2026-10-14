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
from agora.context_memory import ContextMemory, MemoryStateError
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
    RecordRef,
    RecordStatus,
    RequestedPrecision,
    RuntimeOrigin,
    SinkKind,
    SinkStatus,
    SourceKind,
    TurnOutcome,
    Visibility,
    WorkItem,
    WorkState,
    canonical_digest,
    canonical_json_bytes,
    render_add_reaction,
    render_send_message,
)
from agora.fake_sidecar import FakeSidecar
from agora.ports import CrashCheckpoint, FaultInjector, MainModelPort, VirtualClock
from agora.stores import (
    CoreStore,
    IngestReceipt,
    InvalidReferenceError,
    TransportStore,
    ingest_batch,
)

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

    def ingest(
        self,
        transport: TransportStore,
        *,
        consumer_id: str,
        planner: Any = None,
    ) -> IngestReceipt:
        receipt = ingest_batch(
            self.core,
            transport,
            consumer_id=consumer_id,
            ingested_at=self.clock.now_rfc3339(),
            injector=self.injector,
            planner=planner,
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
                    episode_ref=episode_ref,
                    sink_ref=self.sink_ref,
                    effect_capable=True,
                    call_id=call_id,
                    active_view_refs=tuple(held_view_refs),
                )
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
                        reason_codes = []
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
                    if outcome_kind in (OutcomeKind.COMMITTED, OutcomeKind.NO_ACTION):
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
            self.core.put_record(decision)
            outcome_kind = OutcomeKind.HELD
            reason_codes.append(rejected.code)

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
                "UPDATE core_work SET terminal_outcome_ref = ?, state = ?,"
                " lease_owner = NULL, lease_until_ms = NULL WHERE work_id = ?",
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
        refs = [RecordRef(id=effect.id, revision=effect.revision) for effect in effects]
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
