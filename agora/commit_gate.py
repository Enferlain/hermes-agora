"""CommitGate for the M1 slice (handoff §2, §8).

Code checks first, then review routing, then versioned authorization. Every
enabled effect crosses this gate; model output can never submit an
authorization directly. M1 routing is the ordinary slice: NONE for clean
plans (no STRONG commitments exist in fixtures yet — M2 adds routing),
HARD_DENY for deterministic failures. The gate owns no personal-tone
censorship and no alternate send path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from agora.contracts import (
    AudienceSnapshot,
    AuthorizationStatus,
    CapabilityEnvelope,
    CommitAuthorization,
    CommitDecision,
    ComputeSink,
    ContextSnapshot,
    DecisionKind,
    Disposition,
    EffectPlan,
    ReasonCode,
    Record,
    RecordRef,
    SinkKind,
    SinkStatus,
    SnapshotStatus,
    canonical_digest,
)
from agora.ports import VirtualClock
from agora.stores import CoreStore


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


class CommitGate:
    def __init__(self, core: CoreStore, clock: VirtualClock) -> None:
        self.core = core
        self.clock = clock

    def _load(self, ref: RecordRef, cls: type[Record]) -> Record | None:
        record = self.core.get_record(ref.id, ref.revision)
        if record is not None and record.TYPE_NAME != cls.TYPE_NAME:
            return None
        return record

    def evaluate(self, plan: EffectPlan, *, context: GateContext) -> CommitDecision:
        """Handoff §8 order, M1 subset: validate, resolve versions, reject
        deterministic failures, route (NONE in this slice), authorize."""
        try:
            self._validate_plan(plan, context)
        except GateRejection as rejection:
            return self._decision(
                DecisionKind.DENIED,
                reason_codes=(rejection.code,),
            )

        if plan.disposition == Disposition.QUIET:
            return self._decision(
                DecisionKind.QUIET_COMPLETED, reason_codes=(ReasonCode.NO_ACTION,)
            )
        if plan.disposition == Disposition.DEFER:
            return self._decision(DecisionKind.DEFERRED, reason_codes=(ReasonCode.DEFERRED,))

        # ACT with exactly one effect (record validation guarantees the
        # count); route is NONE in the M1 slice, so no assessment is created.
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
        )

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
    ) -> CommitDecision:
        decision = CommitDecision(
            id=f"decision:{len(self._decision_count())}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            kind=kind,
            reason_codes=reason_codes,
            authorization_ref=authorization_ref,
            assessment_ref=None,
        )
        self.core.put_record(decision)
        return decision

    def _decision_count(self) -> list[object]:
        rows = self.core._conn.execute(
            "SELECT record_id FROM core_records WHERE record_type = 'CommitDecision'"
        ).fetchall()
        return [row["record_id"] for row in rows]
