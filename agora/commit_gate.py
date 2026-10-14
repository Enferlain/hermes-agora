"""CommitGate for the replay slice (handoff §2, §8).

Code checks first, then §8 review routing, then versioned authorization.
Every enabled effect crosses this gate; model output can never submit an
authorization directly.

Routing is the §8 first-slice table: HARD_DENY still covers the
deterministic code-check failures (invalid capability/sink/context/bytes);
REQUIRED when an active STRONG commitment in the snapshot, the plan's
runtime producer coverage, or the derived intent needs expression or
observable-use resolution — a QUIET/DEFER resolution under such a
restriction routes REQUIRED too, so the resolution itself is reviewed;
ADVISORY for fixture-configured ordinary concerns (ACT plans only); NONE
otherwise. Routing reads commitment and coverage facts only: sensor scores
have no path into REQUIRED.

REQUIRED builds the closed ReviewEnvelope (the 2026-10-12 note closes the
field set — no release-ledger field), persists the routing decision as a
ReviewRequirement, waits on the scripted ReviewerPort, and binds the
assessment to the exact proposal digest, the routing-time policy/commitment
versions, and the snapshot revision. After the reviewer wait the gate
rechecks those versions; any drift voids the pass (§8.5). Reviewer absence,
failure, or timeout, CONCERNS / INSUFFICIENT_CONTEXT results, non-fact
runtime coverage, and missing comparison views all HOLD the proposal. There
is no downgrade on resubmission: a rewrite is a new proposal_revision whose
route is computed from scratch, and the assessment id embeds the plan id
plus the revision, so a failed assessment is a distinct record that is
never reused.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

from agora.contracts import (
    AssessmentResult,
    AudienceSnapshot,
    AuthorizationStatus,
    CapabilityEnvelope,
    CommitAuthorization,
    CommitDecision,
    Commitment,
    CommitmentStrength,
    ComputeSink,
    ContentFragment,
    ContextSnapshot,
    DecisionKind,
    Disposition,
    EffectPlan,
    Intent,
    MemoryView,
    ProducerCoverage,
    ReasonCode,
    Record,
    RecordRef,
    ReviewAssessment,
    ReviewCoverage,
    ReviewEnvelope,
    ReviewRequirement,
    ReviewRoute,
    SinkKind,
    SinkStatus,
    SnapshotStatus,
    canonical_digest,
    canonical_json_bytes,
)
from agora.ports import ReviewAssessmentData, ReviewerPort, VirtualClock
from agora.stores import CoreStore

#: One assessment per submission is structural (design D4.3/D03): the
#: ReviewAssessment id embeds the plan id + proposal revision.
_ONE_ASSESSMENT_BUDGET = 1


class GateRejection(Exception):
    """Deterministic HARD_DENY with its safe reason code."""

    def __init__(self, code: ReasonCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code


@dataclass(frozen=True, slots=True)
class GateContext:
    """Runtime-resolved versions the gate binds into authorizations."""

    identity_ref: RecordRef
    capability: CapabilityEnvelope
    audience: AudienceSnapshot
    max_message_chars: int


@dataclass(frozen=True, slots=True)
class _Routing:
    """The §8 route plus the facts the route's protocol consumes."""

    route: ReviewRoute
    relevant_commitments: tuple[Commitment, ...]
    policy_refs: tuple[RecordRef, ...]


class CommitGate:
    def __init__(
        self,
        core: CoreStore,
        clock: VirtualClock,
        reviewer: ReviewerPort | None = None,
        *,
        advisory_concerns: Iterable[str] = (),
    ) -> None:
        """``reviewer`` is the scripted review port (handoff §9); REQUIRED
        routes without one hold the proposal (never a PASS on a missing
        review). ``advisory_concerns`` is the fixture-configured set of
        ordinary concerns that routes ACT plans ADVISORY."""
        self.core = core
        self.clock = clock
        self.reviewer = reviewer
        self.advisory_concerns = tuple(advisory_concerns)

    def _load(self, ref: RecordRef, cls: type[Record]) -> Record | None:
        record = self.core.get_record(ref.id, ref.revision)
        if record is not None and record.TYPE_NAME != cls.TYPE_NAME:
            return None
        return record

    def evaluate(self, plan: EffectPlan, *, context: GateContext) -> CommitDecision:
        """Handoff §8 order: validate, route, run the route's review
        protocol, then disposition shortcuts and authorization."""
        try:
            self._validate_plan(plan, context)
        except GateRejection as rejection:
            return self._decision(
                DecisionKind.DENIED,
                reason_codes=(rejection.code,),
            )

        routing = self._route(plan)

        assessment_ref: RecordRef | None = None
        if routing.route == ReviewRoute.REQUIRED:
            passed, hold_codes, assessment_ref = self._required_review(plan, routing)
            if not passed:
                return self._decision(
                    DecisionKind.HOLD,
                    reason_codes=hold_codes,
                    assessment_ref=assessment_ref,
                )
        elif routing.route == ReviewRoute.ADVISORY:
            assessment_ref = self._advisory_review(plan, routing)

        if plan.disposition == Disposition.QUIET:
            return self._decision(
                DecisionKind.QUIET_COMPLETED,
                reason_codes=(ReasonCode.NO_ACTION,),
                assessment_ref=assessment_ref,
            )
        if plan.disposition == Disposition.DEFER:
            return self._decision(
                DecisionKind.DEFERRED,
                reason_codes=(ReasonCode.DEFERRED,),
                assessment_ref=assessment_ref,
            )

        # ACT with exactly one effect (record validation guarantees the
        # count); the route protocol above gated this path.
        effect = plan.effects[0]
        snapshot = self._snapshot(plan)
        authorization = CommitAuthorization(
            id=f"auth:{effect.logical_effect_id}:{plan.proposal_revision}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            logical_effect_id=effect.logical_effect_id,
            proposal_revision=plan.proposal_revision,
            proposal_digest=plan.proposal_digest,
            payload_digest=effect.payload_digest,
            identity_ref=context.identity_ref,
            account_id=effect.account_id,
            audience_disclosure_revision=context.audience.disclosure_context_revision,
            target_dependency_refs=plan.observed_target_refs,
            authority_version_vector=(
                RecordRef(id=context.capability.id, revision=context.capability.revision),
            ),
            policy_version_vector=snapshot.version_vector if snapshot else (),
            runtime_epoch=self.core.runtime_epoch,
            capability_ref=RecordRef(
                id=context.capability.id, revision=context.capability.revision
            ),
            status=AuthorizationStatus.ACTIVE,
        )
        self.core.put_record(authorization)
        return self._decision(
            DecisionKind.AUTHORIZED,
            reason_codes=(),
            authorization_ref=RecordRef(id=authorization.id, revision=authorization.revision),
            assessment_ref=assessment_ref,
        )

    # -- routing (handoff §8 first-slice table) -------------------------------

    def _route(self, plan: EffectPlan) -> _Routing:
        """Route the plan: REQUIRED on an active STRONG restriction in the
        snapshot, the plan's runtime producer coverage, or the derived
        intent (QUIET/DEFER included — the resolution is what gets
        reviewed); ADVISORY for fixture-configured ordinary concerns on ACT
        plans; NONE otherwise."""
        restricting = self._restricting_commitments(plan)
        if restricting:
            return _Routing(
                route=ReviewRoute.REQUIRED,
                relevant_commitments=restricting,
                policy_refs=self._policy_refs(restricting),
            )
        if self.advisory_concerns and plan.disposition == Disposition.ACT:
            return _Routing(route=ReviewRoute.ADVISORY, relevant_commitments=(), policy_refs=())
        return _Routing(route=ReviewRoute.NONE, relevant_commitments=(), policy_refs=())

    def _restricting_commitments(self, plan: EffectPlan) -> tuple[Commitment, ...]:
        """Active STRONG commitments reachable from the plan whose use flags
        restrict expression or observable influence — exactly the §8
        condition 'requires expression/observable-use resolution'. A STRONG
        commitment allowing both dimensions does not force review, and
        ORDINARY_DISCRETION never does. Routing reads these facts only;
        sensor scores have no path here."""
        found: dict[str, RecordRef] = {}
        snapshot = self._snapshot(plan)
        if snapshot is not None:
            self._collect(snapshot.inherited_commitment_refs, found)
            for view_ref in snapshot.active_view_refs:
                view = self._load(view_ref, MemoryView)
                if isinstance(view, MemoryView):
                    self._collect(view.inherited_commitment_refs, found)
        coverage = self._load(plan.producer_coverage_ref, ProducerCoverage)
        if isinstance(coverage, ProducerCoverage):
            for fragment_ref in coverage.input_fragment_refs:
                fragment = self._load(fragment_ref, ContentFragment)
                if isinstance(fragment, ContentFragment):
                    self._collect(fragment.commitment_refs, found)
            for view_ref in coverage.input_view_refs:
                view = self._load(view_ref, MemoryView)
                if isinstance(view, MemoryView):
                    self._collect(view.inherited_commitment_refs, found)
            for intent_ref in coverage.input_intent_refs:
                self._collect_intent(intent_ref, found)
        self._collect_intent(plan.intent_ref, found)
        commitments = [
            commitment
            for commitment in (
                self._load(ref, Commitment) for ref in sorted(found.values(), key=_ref_key)
            )
            if isinstance(commitment, Commitment)
        ]
        return tuple(commitment for commitment in commitments if _restricts(commitment))

    def _collect(self, refs: Iterable[RecordRef], into: dict[str, RecordRef]) -> None:
        for ref in refs:
            if isinstance(ref, RecordRef):
                into[ref.id] = ref

    def _collect_intent(self, intent_ref: RecordRef, into: dict[str, RecordRef]) -> None:
        """Derived intent: its goal fragment's commitments count as plan
        inputs (disclosed-but-unclaimed dependencies travel through the
        runtime coverage, which this scan reads)."""
        intent = self._load(intent_ref, Intent)
        if isinstance(intent, Intent):
            fragment = self._load(intent.goal_fragment_ref, ContentFragment)
            if isinstance(fragment, ContentFragment):
                self._collect(fragment.commitment_refs, into)

    def _policy_refs(self, commitments: Iterable[Commitment]) -> tuple[RecordRef, ...]:
        """The commitments' immutable policy evidence (ResourcePolicy /
        UseAuthorization), deduplicated and deterministically ordered."""
        refs: dict[str, RecordRef] = {}
        for commitment in commitments:
            for ref in commitment.policy_evidence_refs:
                record = self.core.get_record(ref.id, ref.revision)
                evidence = record.TYPE_NAME if record is not None else None
                if evidence in {"ResourcePolicy", "UseAuthorization"}:
                    refs[ref.id] = ref
        return tuple(sorted(refs.values(), key=_ref_key))

    # -- review protocols -----------------------------------------------------

    def _required_review(
        self, plan: EffectPlan, routing: _Routing
    ) -> tuple[bool, tuple[ReasonCode, ...], RecordRef | None]:
        """Handoff §8.4-8.5 REQUIRED protocol: persist the routing decision
        (ReviewRequirement), build the closed ReviewEnvelope, wait on the
        scripted reviewer, then re-verify the bound versions. A pass
        authorizes only when it is keyed to the exact proposal digest, the
        routing-time versions still hold, and the runtime coverage is full
        fact comparison."""
        snapshot = self._snapshot(plan)
        if snapshot is None:
            return False, (ReasonCode.STALE_CONTEXT,), None
        now = self.clock.now_rfc3339()
        suffix = f"{plan.id}:r{plan.proposal_revision}"
        commitment_refs = tuple(
            sorted(
                (
                    RecordRef(id=commitment.id, revision=commitment.revision)
                    for commitment in routing.relevant_commitments
                ),
                key=_ref_key,
            )
        )
        policy_vector = _version_vector(commitment_refs, routing.policy_refs)

        requirement = ReviewRequirement(
            id=f"review-req:{suffix}",
            revision=1,
            created_at=now,
            route=ReviewRoute.REQUIRED,
            reason_codes=(),
            relevant_commitment_refs=commitment_refs,
            expected_coverage=(ReviewCoverage.FACT_COMPARISON,),
            budget=_ONE_ASSESSMENT_BUDGET,
            sink_ref=snapshot.sink_ref,
            policy_version_vector=policy_vector,
        )
        envelope = self._envelope(plan, snapshot, routing, now)
        self.core.put_record(requirement)
        self.core.put_record(envelope)

        assessment = self._assess(plan, envelope, policy_vector=policy_vector, now=now)
        if assessment is None:
            # Reviewer absent, failed, or produced a malformed result: a
            # required review missing its reviewer holds the proposal
            # (invariant I07); nothing passes on a missing review (§9).
            return False, (ReasonCode.REVIEW_TIMEOUT,), None

        if not envelope.comparison_view_refs:
            # A fact-dependent restriction with no authorized comparison
            # view is never evaluated on descriptors alone (spec scenario).
            return False, (ReasonCode.REVIEW_FACTS_MISSING,), _ref(assessment)
        if assessment.result == AssessmentResult.CONCERNS:
            return False, (ReasonCode.REVIEW_CONCERN,), _ref(assessment)
        if assessment.result == AssessmentResult.INSUFFICIENT_CONTEXT:
            return False, (ReasonCode.REVIEW_FACTS_MISSING,), _ref(assessment)
        if assessment.runtime_assigned_coverage != ReviewCoverage.FACT_COMPARISON:
            # A pass claimed without full fact-comparison coverage does not
            # authorize (§8.4: PASS *with full expected runtime coverage*).
            return False, (ReasonCode.REVIEW_FACTS_MISSING,), _ref(assessment)
        if self._versions_changed(policy_vector, plan.snapshot_ref):
            # §8.5: an assessment against changed versions does not
            # authorize the current bytes.
            return False, (ReasonCode.POLICY_CHANGED,), _ref(assessment)
        return True, (), _ref(assessment)

    def _advisory_review(self, plan: EffectPlan, routing: _Routing) -> RecordRef | None:
        """Handoff §8.4 ADVISORY: record bounded advice; it never blocks
        delivery (invariant I07), and a reviewer failure leaves only the
        unadvised requirement — advice is never manufactured."""
        snapshot = self._snapshot(plan)
        if snapshot is None:
            return None
        now = self.clock.now_rfc3339()
        suffix = f"{plan.id}:r{plan.proposal_revision}"
        requirement = ReviewRequirement(
            id=f"review-req:{suffix}",
            revision=1,
            created_at=now,
            route=ReviewRoute.ADVISORY,
            reason_codes=(ReasonCode.REVIEW_CONCERN,),
            relevant_commitment_refs=(),
            expected_coverage=(),
            budget=_ONE_ASSESSMENT_BUDGET,
            sink_ref=snapshot.sink_ref,
            policy_version_vector=(),
        )
        envelope = self._envelope(plan, snapshot, routing, now)
        self.core.put_record(requirement)
        self.core.put_record(envelope)
        assessment = self._assess(plan, envelope, policy_vector=(), now=now)
        if assessment is None:
            return None
        return _ref(assessment)

    def _envelope(
        self,
        plan: EffectPlan,
        snapshot: ContextSnapshot,
        routing: _Routing,
        now: str,
    ) -> ReviewEnvelope:
        """The closed §4.4 envelope: exact proposal digest, audience/intent
        refs, immutable relevant policies and commitments, authorized
        comparison views (the snapshot's active views), bounded quote
        fragments, runtime-assigned coverage refs, sink, and the exact
        messages payload out of line."""
        suffix = f"{plan.id}:r{plan.proposal_revision}"
        payload_id = f"payload:review:{suffix}"
        messages = canonical_json_bytes(
            [
                {"role": "system", "content": "semantic review required before commit"},
                {
                    "role": "user",
                    "content": canonical_json_bytes(
                        {
                            "plan": {"id": plan.id, "revision": plan.revision},
                            "proposal_digest": plan.proposal_digest,
                            "route": routing.route.value,
                            "relevant_commitments": [
                                commitment.id for commitment in routing.relevant_commitments
                            ],
                            "comparison_views": [ref.id for ref in snapshot.active_view_refs],
                            "expected_coverage": (
                                [ReviewCoverage.FACT_COMPARISON.value]
                                if routing.route == ReviewRoute.REQUIRED
                                else []
                            ),
                        }
                    ).decode("utf-8"),
                },
            ]
        )
        self.core.put_payload(payload_id, messages.decode("utf-8"), owner_ref=f"plan:{plan.id}")
        return ReviewEnvelope(
            id=f"review:{suffix}",
            revision=1,
            created_at=now,
            plan_ref=RecordRef(id=plan.id, revision=plan.revision),
            proposal_digest=plan.proposal_digest,
            snapshot_ref=plan.snapshot_ref,
            audience_ref=snapshot.audience_ref,
            intent_ref=plan.intent_ref,
            relevant_policy_refs=routing.policy_refs,
            relevant_commitment_refs=tuple(
                sorted(
                    (
                        RecordRef(id=commitment.id, revision=commitment.revision)
                        for commitment in routing.relevant_commitments
                    ),
                    key=_ref_key,
                )
            ),
            comparison_view_refs=snapshot.active_view_refs,
            bounded_quote_fragment_refs=snapshot.selected_fragment_refs,
            expected_coverage_refs=(plan.producer_coverage_ref,),
            sink_ref=snapshot.sink_ref,
            exact_messages_payload_ref=payload_id,
        )

    def _assess(
        self,
        plan: EffectPlan,
        envelope: ReviewEnvelope,
        *,
        policy_vector: tuple[RecordRef, ...],
        now: str,
    ) -> ReviewAssessment | None:
        """Wait on the scripted reviewer and bind its result into the
        assessment record (id embeds plan id + revision). Any reviewer
        absence, error, or malformed result returns None — a HOLD, never a
        PASS — so the §9 forbidden fallback ('PASS on exception/missing
        facts') is structural."""
        if self.reviewer is None:
            return None
        try:
            data: ReviewAssessmentData = self.reviewer.assess(envelope)
            assessment = ReviewAssessment(
                id=f"assessment:{plan.id}:r{plan.proposal_revision}",
                revision=1,
                created_at=now,
                result=AssessmentResult(data.result),
                flags=tuple(data.flags),
                runtime_assigned_coverage=ReviewCoverage(data.runtime_assigned_coverage),
                supplied_view_refs=envelope.comparison_view_refs,
                proposal_digest=plan.proposal_digest,
                policy_version_vector=policy_vector,
                snapshot_revision=envelope.snapshot_ref.revision,
            )
        except Exception:
            return None
        self.core.put_record(assessment)
        return assessment

    def _versions_changed(self, vector: tuple[RecordRef, ...], snapshot_ref: RecordRef) -> bool:
        """§8.5 recheck after the model wait: the assessment authorizes only
        against unchanged versions — every bound record still at its bound
        revision, and the snapshot still the current, active one."""
        for ref in vector:
            current = self.core.get_record(ref.id)
            if current is None or current.revision != ref.revision:
                return True
        snapshot = self.core.get_record(snapshot_ref.id)
        return (
            not isinstance(snapshot, ContextSnapshot)
            or snapshot.status != SnapshotStatus.ACTIVE
            or snapshot.revision != snapshot_ref.revision
        )

    # -- plan validation (unchanged M1 HARD_DENY path) ------------------------

    def _validate_plan(self, plan: EffectPlan, context: GateContext) -> None:
        capability = context.capability
        if capability.status.value != "ACTIVE":
            raise GateRejection(ReasonCode.CAPABILITY_INVALID, "capability not active")
        if plan.disposition != Disposition.ACT:
            return
        effect = plan.effects[0]
        if effect.capability_ref.id != capability.id:
            raise GateRejection(ReasonCode.CAPABILITY_INVALID, "effect cites another capability")
        if effect.kind not in capability.allowed_effect_kinds:
            raise GateRejection(
                ReasonCode.UNSUPPORTED, f"effect kind {effect.kind.value} not allowed"
            )
        if effect.destination_id not in capability.allowed_destination_ids:
            raise GateRejection(ReasonCode.CAPABILITY_INVALID, "destination not allowed")
        if effect.account_id != capability.account_id:
            raise GateRejection(ReasonCode.CAPABILITY_INVALID, "account mismatch")

        snapshot = self._snapshot(plan)
        if snapshot is None or snapshot.status != SnapshotStatus.ACTIVE:
            raise GateRejection(ReasonCode.STALE_CONTEXT, "snapshot missing or invalid")
        if not snapshot.effect_capable:
            raise GateRejection(ReasonCode.CONTEXT_MISMATCH, "snapshot not effect-capable")
        sink = self._load(snapshot.sink_ref, ComputeSink)
        if not isinstance(sink, ComputeSink):
            raise GateRejection(ReasonCode.SINK_FORBIDDEN, "compute sink missing")
        if sink.status != SinkStatus.ENABLED or sink.kind != SinkKind.SCRIPTED_TEST:
            raise GateRejection(ReasonCode.SINK_FORBIDDEN, "sink not an enabled scripted test sink")

        # Exact rendered bytes: recompute the digest from the payload store.
        rendered = self.core.get_payload(effect.rendered_payload_ref)
        if rendered is None:
            raise GateRejection(ReasonCode.CONFLICT, "rendered payload missing")
        if canonical_digest(json.loads(rendered)) != effect.payload_digest:
            raise GateRejection(ReasonCode.CONFLICT, "payload digest mismatch")

    def _snapshot(self, plan: EffectPlan) -> ContextSnapshot | None:
        snapshot = self.core.get_record(plan.snapshot_ref.id, plan.snapshot_ref.revision)
        if isinstance(snapshot, ContextSnapshot):
            return snapshot
        return None

    def _decision(
        self,
        kind: DecisionKind,
        *,
        reason_codes: tuple[ReasonCode, ...],
        authorization_ref: RecordRef | None = None,
        assessment_ref: RecordRef | None = None,
    ) -> CommitDecision:
        decision = CommitDecision(
            id=f"decision:{len(self._decision_count())}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            kind=kind,
            reason_codes=reason_codes,
            authorization_ref=authorization_ref,
            assessment_ref=assessment_ref,
        )
        self.core.put_record(decision)
        return decision

    def _decision_count(self) -> list[object]:
        rows = self.core._conn.execute(
            "SELECT record_id FROM core_records WHERE record_type = 'CommitDecision'"
        ).fetchall()
        return [row["record_id"] for row in rows]


def _restricts(commitment: Commitment) -> bool:
    """STRONG strength plus a closed expression or observable-influence
    dimension — the resolution such a restriction needs is the review."""
    return commitment.strength == CommitmentStrength.STRONG and not (
        commitment.expression_allowed and commitment.observable_influence_allowed
    )


def _version_vector(*groups: Iterable[RecordRef]) -> tuple[RecordRef, ...]:
    """Unique-id, id-sorted version vector (handoff §3 header rules)."""
    by_id: dict[str, RecordRef] = {}
    for group in groups:
        for ref in group:
            by_id[ref.id] = ref
    return tuple(sorted(by_id.values(), key=_ref_key))


def _ref(record: Record) -> RecordRef:
    return RecordRef(id=record.id, revision=record.revision)


def _ref_key(ref: RecordRef) -> str:
    return f"{ref.id}:{ref.revision}"
