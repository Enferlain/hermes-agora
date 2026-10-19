"""Context assembly and scoped memory (handoff §5/§6): snapshot building
with structural view admission, episode-scoped selection, envelopes and
producer coverage, staged memory with corrections and dependency
invalidation, and runtime-origin-bound PROPOSE_MEMORY lineage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from agora.contracts import (
    AccessStatus,
    AssertionStatus,
    AudienceSnapshot,
    AuthorityBinding,
    Commitment,
    CommitmentStrength,
    ContentFragment,
    ContextSnapshot,
    ConversationEpisode,
    DependencyKind,
    Derivation,
    DerivationState,
    EligibilityStatus,
    EnvelopePurpose,
    ExposureClass,
    FragmentKind,
    InboundEvent,
    InstructionAuthority,
    Integrity,
    IntendedUse,
    Intent,
    MemoryAccessRequest,
    MemoryAccessResult,
    MemoryRecord,
    MemoryStage,
    MemoryStatus,
    MemoryView,
    ModelCallEnvelope,
    Pair,
    ProducerCoverage,
    ProducerKind,
    Provenance,
    ReasonCode,
    Record,
    RecordRef,
    RecordStatus,
    ResourcePolicy,
    RuntimeOrigin,
    ScopeKind,
    SnapshotStatus,
    SourceKind,
    TransformCertificate,
    UseAuthorization,
    ValidationError,
    canonical_json_bytes,
    strict_json_loads,
)
from agora.ports import CrashCheckpoint, FaultInjector, VirtualClock
from agora.stores import CoreStore
from agora.transforms import discharged_by, transform_spec

from .evaluation import (
    _VIEW_KIND_FOR_PRECISION,
    AccessVerdict,
    AuthorityEvaluator,
    EvaluationInput,
    _verdict,
)

MAX_SELECTED_EVENTS = 20
MAX_CONTEXT_BYTES = 16 * 1024

_STAGE_ORDER: dict[MemoryStage, int] = {
    MemoryStage.EPHEMERAL: 0,
    MemoryStage.SOCIAL: 1,
    MemoryStage.CANDIDATE: 2,
    MemoryStage.DURABLE: 3,
}


class MemoryStateError(Exception):
    """Typed memory-lifecycle failure with a registry reason code."""

    def __init__(self, code: ReasonCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code


class ViewAdmissionError(Exception):
    """Structural view-admission failure at snapshot build (task 3.3): the
    containment decision happens here — never at dispatch, and it is not a
    reviewer-waivable exception."""

    def __init__(self, code: ReasonCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code


def _vector_cites(vector: tuple[RecordRef, ...], record_id: str, revision: int) -> bool:
    return any(ref.id == record_id and ref.revision == revision for ref in vector)


class ContextMemory:
    def __init__(
        self,
        core: CoreStore,
        clock: VirtualClock,
        injector: FaultInjector | None = None,
        *,
        serializer_version: str = "ser:v1",
    ) -> None:
        self.core = core
        self.clock = clock
        self.injector = injector
        self.serializer_version = serializer_version

    def _trip(self, checkpoint: CrashCheckpoint) -> None:
        if self.injector is not None:
            self.injector.trip(checkpoint)

    # -- protected access (M2 design D1: the record-building shell) ---------

    def _current_of[R: Record](self, cls: type[R]) -> tuple[R, ...]:
        """Current revisions of one record type, in id order (deterministic)."""
        rows = self.core._conn.execute(
            "SELECT record_id, revision FROM core_records WHERE record_type = ?"
            " AND is_current = 1 ORDER BY record_id",
            (cls.TYPE_NAME,),
        ).fetchall()
        return tuple(
            record
            for row in rows
            if isinstance(record := self.core.get_record(row["record_id"], row["revision"]), cls)
        )

    def _fragment_or_none(self, record_id: str) -> ContentFragment | None:
        record = self.core.get_record(record_id)
        return record if isinstance(record, ContentFragment) else None

    def request_access(self, request: MemoryAccessRequest) -> MemoryAccessResult:
        """Persist the request (necessity text out-of-line), evaluate
        authority independently, and persist the typed result — plus, when
        granted, the restricted MemoryView citing its authorization chain
        and inherited commitments. The evaluation itself never sees payload
        bytes; a non-GRANTED result persists no view, no permitted uses,
        and no authorizations (handoff §5, D07)."""
        now = self.clock.now_rfc3339()
        self.core.put_record(request, text_field_refs={"necessity_text": "necessity_payload_ref"})
        intent = self.core.get_record(request.intent_ref.id, request.intent_ref.revision)
        audience = self.core.get_record(request.audience_ref.id, request.audience_ref.revision)
        all_policies = self._current_of(ResourcePolicy)
        all_bindings = self._current_of(AuthorityBinding)
        all_authorizations = self._current_of(UseAuthorization)
        all_commitments = self._current_of(Commitment)

        evaluator = AuthorityEvaluator()
        if request.resource_id is not None:
            cases = (
                EvaluationInput(
                    intended_use=request.intended_use,
                    requested_precision=request.requested_precision,
                    transform_id=request.transform_id,
                    intent=intent if isinstance(intent, Intent) else None,
                    audience=audience if isinstance(audience, AudienceSnapshot) else None,
                    resource=self._fragment_or_none(request.resource_id),
                    policies=tuple(p for p in all_policies if p.resource_id == request.resource_id),
                    bindings=all_bindings,
                    authorizations=all_authorizations,
                    commitments=all_commitments,
                    now=now,
                ),
            )
            class_resource_id = request.resource_id
        else:
            assert request.data_class is not None
            # One case per MEMBER RESOURCE (all its policies together), not
            # per policy — duplicate active policies on one resource must
            # surface the CONFLICT verdict on the class path too (review).
            grouped: dict[str, list[ResourcePolicy]] = {}
            for policy in all_policies:
                if policy.data_class == request.data_class:
                    grouped.setdefault(policy.resource_id, []).append(policy)
            cases = tuple(
                EvaluationInput(
                    intended_use=request.intended_use,
                    requested_precision=request.requested_precision,
                    transform_id=request.transform_id,
                    intent=intent if isinstance(intent, Intent) else None,
                    audience=audience if isinstance(audience, AudienceSnapshot) else None,
                    resource=self._fragment_or_none(resource_id),
                    policies=tuple(sorted(policies, key=lambda p: p.id)),
                    bindings=all_bindings,
                    authorizations=all_authorizations,
                    commitments=all_commitments,
                    now=now,
                )
                for resource_id, policies in sorted(grouped.items())
            )
            class_resource_id = request.data_class
        assert class_resource_id is not None
        verdict = (
            evaluator.evaluate(cases[0])
            if request.resource_id is not None
            else evaluator.evaluate_class(cases)
        )

        coverage = ProducerCoverage(
            id=f"coverage:access:{request.request_id}",
            revision=1,
            created_at=now,
            producer_kind=ProducerKind.CONTEXT_ASSEMBLY,
            input_fragment_refs=tuple(
                RecordRef(id=case.resource.id, revision=case.resource.revision)
                for case in cases
                if case.resource is not None
            ),
            input_view_refs=(),
            input_intent_refs=(request.intent_ref,),
            input_policy_refs=tuple(
                RecordRef(id=policy.id, revision=policy.revision)
                for case in cases
                for policy in case.policies
            ),
            sink_ref=None,
            call_id=f"access:{request.request_id}",
            runtime_epoch=self.core.runtime_epoch,
        )

        granted = verdict.status == AccessStatus.GRANTED
        view: MemoryView | None = None
        if granted:
            authorization_refs = tuple(
                RecordRef(id=auth.id, revision=auth.revision) for auth in verdict.authorizations
            )
            # With several merged authorizations (data-class grants) the
            # bounds are the INTERSECTION: the view is only valid where
            # every granting member allows it (fail-closed).
            bounds_sets = [set(auth.audience_class_bounds) for auth in verdict.authorizations]
            audience_bounds = tuple(sorted(set.intersection(*bounds_sets))) if bounds_sets else ()
            valid_until = min(
                (
                    auth.valid_until
                    for auth in verdict.authorizations
                    if auth.valid_until is not None
                ),
                default=None,
            )
            view = MemoryView(
                id=f"view:{request.request_id}",
                revision=1,
                created_at=now,
                resource_id=class_resource_id,
                returned_fragment_refs=tuple(
                    RecordRef(id=case.resource.id, revision=case.resource.revision)
                    for case in cases
                    if case.resource is not None
                ),
                authorization_refs=authorization_refs,
                transform_certificate_ref=None,
                producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
                precision=request.requested_precision,
                inherited_commitment_refs=tuple(
                    RecordRef(id=c.id, revision=c.revision) for c in verdict.inherited_commitments
                ),
                permitted_uses=verdict.permitted_uses,
                audience_bounds=audience_bounds,
                valid_until=valid_until,
                status=RecordStatus.ACTIVE,
            )
        certificate: TransformCertificate | None = None
        transform_records: tuple[Record, ...] = ()
        source_view = view
        if granted and view is not None and request.transform_id is not None:
            try:
                view, certificate, transform_records = self._apply_transform(
                    request, view, verdict, now
                )
            except ValueError, ValidationError:
                # Authority granted but the transform cannot run on this
                # input (multi-member source, non-JSON payload, missing
                # parameters): a typed no-data result, never an aborted
                # turn with a dangling request (review).
                view = None
                certificate = None
                transform_records = ()
                source_view = None
                verdict = _verdict(AccessStatus.DENIED, ReasonCode.UNSUPPORTED)
                granted = False
        result = MemoryAccessResult(
            id=f"result:{request.request_id}",
            revision=1,
            created_at=now,
            request_id=request.request_id,
            access_status=verdict.status,
            view_kind=(
                _VIEW_KIND_FOR_PRECISION[view.precision] if view is not None else verdict.view_kind
            ),
            view_ref=RecordRef(id=view.id, revision=view.revision) if view else None,
            reason_codes=verdict.reason_codes,
            safe_feedback_template_id=None,
            permitted_uses=verdict.permitted_uses,
            producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
            transform_certificate_ref=(
                RecordRef(id=certificate.id, revision=certificate.revision) if certificate else None
            ),
            authorization_refs=tuple(
                RecordRef(id=auth.id, revision=auth.revision) for auth in verdict.authorizations
            )
            if granted
            else (),
            inherited_commitment_refs=tuple(
                RecordRef(id=c.id, revision=c.revision) for c in verdict.inherited_commitments
            )
            if granted
            else (),
        )
        batch: tuple[Record, ...] = (
            *((coverage, source_view) if source_view else (coverage,)),
            *transform_records,
            result,
        )
        # the batch AND the transform-output edge commit as ONE unit: a
        # crash can no more leave the derived view durable without its
        # cascade edge (the fail-open shape hermes-agora-5e2 closed for
        # snapshots) than leave the batch half-written
        with self.core.transaction() as connection:
            for record in batch:
                self.core._put_record_tx(connection, record)
            self.core._verify_ref_targets_tx(connection, batch)
            if source_view is not None and certificate is not None:
                # the transform output derives from the granted view: the
                # edge makes revocation of the source cascade to it (I14
                # late derivations, hermes-agora-5e2)
                self.core._add_dependency_tx(
                    connection,
                    certificate.output_view_ref,
                    RecordRef(id=source_view.id, revision=source_view.revision),
                    DependencyKind.PRODUCER_INPUT,
                )
        if not granted and verdict.reason_codes:
            # Replay-dedup: records re-insert byte-identically, but the
            # audit log has no natural key — skip the row if the same
            # denial was already audited for this result (review).
            already = self.core._conn.execute(
                "SELECT 1 FROM core_audit WHERE entity_ref = ? AND reason_code = ? LIMIT 1",
                (result.id, verdict.reason_codes[0].value),
            ).fetchone()
            if already is None:
                self.core.audit(
                    operation="access",
                    entity_ref=result.id,
                    reason_code=verdict.reason_codes[0].value,
                    version_refs=(request.id,),
                    at_ms=self.clock.now_ms(),
                )
        return result

    def _apply_transform(
        self,
        request: MemoryAccessRequest,
        source_view: MemoryView,
        verdict: AccessVerdict,
        now: str,
    ) -> tuple[MemoryView, TransformCertificate, tuple[Record, ...]]:
        """Run one registry transform over the authorized source view (design
        D3): compute the output out-of-line, write a TransformCertificate
        naming discharged vs retained commitments (only the spec's discharge
        rule names anything), and build the output view citing the
        certificate. Output views are internal-use only in this slice — a
        predicate may itself reveal finances, so no transform widens uses."""
        spec = transform_spec(request.transform_id) if request.transform_id else None
        if spec is None:
            raise ValueError("transform application requires a transform id")
        if len(source_view.returned_fragment_refs) != 1:
            raise ValueError("a transform applies to exactly one source resource")
        parameters = json.loads(self.core.get_meta("fixture_transforms") or "{}").get(
            spec.transform_id
        )
        if parameters is None:
            raise ValueError(f"no fixture parameters seeded for {spec.transform_id}")
        source_fragment = self._fragment_or_none(source_view.returned_fragment_refs[0].id)
        if source_fragment is None:
            raise ValueError("source fragment missing")
        payload_text = self.core.get_payload(source_fragment.payload_ref)
        if payload_text is None:
            raise ValueError("source payload unavailable")
        payload = strict_json_loads(payload_text)
        output = spec.run(payload, parameters)
        output_payload_id = f"payload:transform:{request.request_id}"
        self.core.put_payload(
            output_payload_id,
            canonical_json_bytes(output).decode("utf-8"),
            owner_ref=f"view:{request.request_id}",
        )

        inherited = verdict.inherited_commitments
        discharged = tuple(c for c in inherited if discharged_by(spec, c))
        retained = tuple(c for c in inherited if not discharged_by(spec, c))
        authorization_refs = tuple(
            RecordRef(id=auth.id, revision=auth.revision) for auth in verdict.authorizations
        )
        binding_refs = tuple(
            RecordRef(id=binding.id, revision=binding.revision) for binding in verdict.bindings
        )
        coverage = ProducerCoverage(
            id=f"coverage:transform:{request.request_id}",
            revision=1,
            created_at=now,
            producer_kind=ProducerKind.TRANSFORM,
            input_fragment_refs=(
                RecordRef(id=source_fragment.id, revision=source_fragment.revision),
            ),
            input_view_refs=(RecordRef(id=source_view.id, revision=source_view.revision),),
            input_intent_refs=(request.intent_ref,),
            input_policy_refs=source_view.authorization_refs,
            sink_ref=None,
            call_id=f"transform:{request.request_id}",
            runtime_epoch=self.core.runtime_epoch,
        )
        output_fragment = ContentFragment(
            id=f"fragment:transform:{request.request_id}",
            revision=1,
            created_at=now,
            kind=FragmentKind.TEXT,
            payload_ref=output_payload_id,
            provenance=Provenance(
                producer_ref=RecordRef(id="principal:agent", revision=1),
                source_kind=SourceKind.TOOL_RESULT,
                origin_event_ref=None,
                claimed_speaker_ref=None,
                instruction_authority=InstructionAuthority.AGENT_STATE,
                integrity=Integrity.DERIVED,
                custodian_refs=(),
                subject_refs=(),
                original_audience_ref=None,
                assertion_status=AssertionStatus.INFERENCE,
                producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
                model_claimed_parent_refs=(
                    RecordRef(id=source_fragment.id, revision=source_fragment.revision),
                ),
                occurred_at=now,
                observed_at=now,
            ),
            commitment_refs=tuple(RecordRef(id=c.id, revision=c.revision) for c in retained),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
        certificate = TransformCertificate(
            id=f"certificate:{request.request_id}",
            revision=1,
            created_at=now,
            registry_transform_id=spec.transform_id,
            parameter_values=tuple(
                Pair(first=name, second=value) for name, value in parameters.items()
            ),
            source_coverage_ref=source_view.producer_coverage_ref,
            authorizing_binding_refs=binding_refs,
            authorizing_use_refs=authorization_refs,
            discharged_commitment_refs=tuple(
                RecordRef(id=c.id, revision=c.revision) for c in discharged
            ),
            retained_commitment_refs=tuple(
                RecordRef(id=c.id, revision=c.revision) for c in retained
            ),
            output_view_ref=RecordRef(id=f"view:{request.request_id}:transform", revision=1),
        )
        output_view = MemoryView(
            id=f"view:{request.request_id}:transform",
            revision=1,
            created_at=now,
            resource_id=source_view.resource_id,
            returned_fragment_refs=(
                RecordRef(id=output_fragment.id, revision=output_fragment.revision),
            ),
            authorization_refs=authorization_refs,
            transform_certificate_ref=RecordRef(id=certificate.id, revision=certificate.revision),
            producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
            precision=spec.output_precision,
            inherited_commitment_refs=tuple(
                RecordRef(id=c.id, revision=c.revision) for c in retained
            ),
            permitted_uses=(IntendedUse.INTERNAL_ONLY,),
            audience_bounds=source_view.audience_bounds,
            valid_until=source_view.valid_until,
            status=RecordStatus.ACTIVE,
        )
        return output_view, certificate, (coverage, output_fragment, certificate, output_view)

    def eligible_episode_fragments(
        self, episode_ref: RecordRef
    ) -> tuple[tuple[RecordRef, ...], int]:
        """Fragments of the episode's member events, newest last, and the
        bytes they consumed. Membership lives on
        ``ConversationEpisode.member_event_refs`` (handoff §5), maintained
        at ingest — members confer no scope rights, they only narrow
        context, so a foreign episode's fragments can never enter this
        snapshot. The episode is read at its pinned revision (as-of-turn
        membership); append order is journal order, so the newest
        MAX_SELECTED_EVENTS members are the episode's latest events, and
        the budget is SHARED with memory fragments (handoff §6.4: one
        16 KiB serialized social-content limit)."""
        episode = self.core.get_record(episode_ref.id, episode_ref.revision)
        if not isinstance(episode, ConversationEpisode):
            return (), 0
        members = episode.member_event_refs[-MAX_SELECTED_EVENTS:]
        selected: list[RecordRef] = []
        total = 0
        for member in reversed(members):
            event_record = self.core.get_record(member.id, member.revision)
            if not isinstance(event_record, InboundEvent):
                continue
            for ref in event_record.fragment_refs:
                fragment_record = self.core.get_record(ref.id, ref.revision)
                if not isinstance(fragment_record, ContentFragment):
                    continue
                # size via the fragment's own payload ref — the previous
                # convention-based lookup never matched the runner's payload
                # ids, silently zeroing the budget (latent M1 bug, flushed
                # out by the section-3 review)
                size = self._payload_size(fragment_record.payload_ref)
                if total + size > MAX_CONTEXT_BYTES:
                    continue  # omit excess whole fragments, keep metadata
                total += size
                selected.append(ref)
        return tuple(reversed(selected)), total

    def _eligible_memory_fragments(
        self, destination_id: str, remaining_bytes: int
    ) -> tuple[RecordRef, ...]:
        """Eligible memory assertion fragments for one destination scope,
        within the REMAINING shared context byte budget. Eligibility is
        filtered on the scope index before any payload is read;
        deterministically in memory id order."""
        memory_refs = self.core.eligible_memory_refs(
            scope_kind=ScopeKind.ROOM.value,
            scope_ref=destination_id,
            now_ms=self.clock.now_ms(),
            stages=frozenset({MemoryStage.SOCIAL, MemoryStage.CANDIDATE, MemoryStage.DURABLE}),
        )
        selected: list[RecordRef] = []
        total = 0
        for memory_ref in memory_refs:
            record = self.core.get_record(memory_ref.id, memory_ref.revision)
            if not isinstance(record, MemoryRecord):
                continue
            fragment_record = self.core.get_record(
                record.assertion_fragment_ref.id, record.assertion_fragment_ref.revision
            )
            if not isinstance(fragment_record, ContentFragment):
                continue
            if fragment_record.eligibility_status != EligibilityStatus.ACTIVE:
                continue  # superseded/tombstoned assertions never re-enter
            size = self._payload_size(fragment_record.payload_ref)
            if total + size > remaining_bytes:
                continue  # omit excess whole fragments, keep metadata
            total += size
            selected.append(RecordRef(id=fragment_record.id, revision=fragment_record.revision))
        return tuple(selected)

    def _payload_size(self, payload_id: str) -> int:
        row = self.core._conn.execute(
            "SELECT length(data) AS n FROM core_payloads WHERE payload_id = ?",
            (payload_id,),
        ).fetchone()
        return row["n"] if row is not None else 0

    # -- staged promotion and corrections (M2 task 3.2, §6.3) ---------------

    def propose_memory(
        self,
        *,
        assertion_text: str,
        source_refs: tuple[RecordRef, ...],
        requested_stage: MemoryStage,
        requested_scope_kind: ScopeKind,
        scope_ref: str,
        existing_memory_ref: RecordRef | None,
        runtime_origin: RuntimeOrigin,
    ) -> RecordRef:
        """Explicit PROPOSE_MEMORY (handoff §5, §6.4): retain an ordinary
        observation as a scoped memory, or promote an existing one. The
        runtime origin is coordinator-minted — the model never sets it — and
        lineage is verified against that origin's producer coverage: the
        model may only cite fragments the runtime actually supplied, so a
        forged or declared-parent-omitted citation is a typed
        INVALID_REFERENCE, never a silent write. Storage inherits every
        source commitment — the cited fragments' own plus any carried by the
        granted views that returned them (a request cannot discharge
        commitments) — and is refused when a STRONG commitment forbids
        internal use. Scope is never widened: an ordinary observation is
        retained in its source room (the turn's destination) or as the
        agent's own; BROAD needs a curated scope-change permission that
        does not exist in this slice, DURABLE needs an existing CANDIDATE,
        and promotions carry the existing scope unchanged."""
        if existing_memory_ref is not None:
            memory = self._current_memory_or_error(existing_memory_ref)
            if (requested_scope_kind, scope_ref) != (memory.scope_kind, memory.scope_ref):
                raise MemoryStateError(
                    ReasonCode.POLICY_FORBIDDEN,
                    "promotion cannot move scope; broad promotion needs a curated permission",
                )
            return self.stage_memory(existing_memory_ref, requested_stage)
        if requested_stage is MemoryStage.DURABLE:
            raise MemoryStateError(
                ReasonCode.POLICY_FORBIDDEN,
                "DURABLE requires an existing CANDIDATE memory and an explicit proposal",
            )
        if requested_scope_kind is ScopeKind.BROAD:
            raise MemoryStateError(
                ReasonCode.POLICY_FORBIDDEN,
                "BROAD scope requires an explicit curated scope-change permission",
            )
        self._check_memory_scope(requested_scope_kind, scope_ref, runtime_origin)
        supplied = self._runtime_supplied_refs(runtime_origin)
        for ref in source_refs:
            if ref not in supplied:
                raise MemoryStateError(
                    ReasonCode.INVALID_REFERENCE,
                    f"source {ref.id} was not supplied by the runtime context",
                )
        inherited: dict[str, RecordRef] = {}
        for ref in sorted(source_refs, key=lambda r: r.id):
            fragment = self.core.get_record(ref.id, ref.revision)
            assert isinstance(fragment, ContentFragment)
            for commitment_ref in fragment.commitment_refs:
                inherited[commitment_ref.id] = commitment_ref
        # A fragment obtained through a granted view carries that view's
        # inherited commitments too — the authorization chain's restrictions
        # (which may exceed the fragment's own) travel with the bytes, so
        # storage cannot launder them by citing only fragment-level
        # commitments (review finding on propose_memory inheritance).
        coverage = self._origin_coverage(runtime_origin)
        for view_ref in coverage.input_view_refs:
            view = self.core.get_record(view_ref.id, view_ref.revision)
            if not isinstance(view, MemoryView):
                continue
            if any(ref in view.returned_fragment_refs for ref in source_refs):
                for commitment_ref in view.inherited_commitment_refs:
                    inherited[commitment_ref.id] = commitment_ref
        for commitment_ref in sorted(inherited.values(), key=lambda r: r.id):
            commitment = self.core.get_record(commitment_ref.id, commitment_ref.revision)
            if (
                isinstance(commitment, Commitment)
                and commitment.strength == CommitmentStrength.STRONG
                and not commitment.internal_use_allowed
            ):
                raise MemoryStateError(
                    ReasonCode.POLICY_FORBIDDEN,
                    f"source {commitment_ref.id} forbids internal use; no storage policy matches",
                )
        now = self.clock.now_rfc3339()
        memory_id = (
            "memory:propose:"
            + hashlib.sha256(
                canonical_json_bytes(
                    {
                        "assertion": assertion_text,
                        "sources": [ref.to_payload() for ref in source_refs],
                        "stage": requested_stage.value,
                        "scope": [requested_scope_kind.value, scope_ref],
                    }
                )
            ).hexdigest()[:24]
        )
        existing = self.core.current_ref(memory_id)
        if existing is not None:
            # Crash replay re-proposes byte-identical content: the stored
            # revision is the answer, never a duplicate.
            return existing
        fragment_id = f"fragment:{memory_id}"
        self.core.put_payload(f"payload:{fragment_id}", assertion_text, owner_ref=memory_id)
        fragment = ContentFragment(
            id=fragment_id,
            revision=1,
            created_at=now,
            kind=FragmentKind.TEXT,
            payload_ref=f"payload:{fragment_id}",
            provenance=Provenance(
                producer_ref=runtime_origin.actor_ref,
                source_kind=SourceKind.MEMORY,
                origin_event_ref=None,
                claimed_speaker_ref=None,
                instruction_authority=InstructionAuthority.AGENT_STATE,
                integrity=Integrity.DERIVED,
                custodian_refs=(),
                subject_refs=(),
                original_audience_ref=None,
                assertion_status=AssertionStatus.AGENT_ADOPTED_PREFERENCE,
                producer_coverage_ref=runtime_origin.producer_coverage_ref,
                model_claimed_parent_refs=tuple(source_refs),
                occurred_at=now,
                observed_at=now,
            ),
            commitment_refs=tuple(sorted(inherited.values(), key=lambda r: r.id)),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
        memory = MemoryRecord(
            id=memory_id,
            revision=1,
            created_at=now,
            assertion_fragment_ref=RecordRef(id=fragment.id, revision=fragment.revision),
            stage=requested_stage,
            scope_kind=requested_scope_kind,
            scope_ref=scope_ref,
            custodian_refs=(),
            subject_refs=(),
            commitment_refs=tuple(sorted(inherited.values(), key=lambda r: r.id)),
            assertion_status=AssertionStatus.AGENT_ADOPTED_PREFERENCE,
            supersedes_ref=None,
            valid_until=None,
            status=MemoryStatus.ACTIVE,
        )
        self.core.put_record(fragment)
        self.core.promote_memory(memory, expiry_ms=None)
        return RecordRef(id=memory.id, revision=memory.revision)

    def _check_memory_scope(
        self, scope_kind: ScopeKind, scope_ref: str, runtime_origin: RuntimeOrigin
    ) -> None:
        """Scope is bound to the turn, not chosen freely (handoff §6.4:
        ordinary observations are retained 'in their source room'): an
        AGENT-scoped memory is the agent's own and always allowed; a
        ROOM-scoped memory must name the destination of the context the
        runtime actually supplied. RELATIONSHIP and PROJECT have no curated
        mapping in this slice and are refused rather than guessed."""
        if scope_kind is ScopeKind.AGENT:
            return
        if scope_kind is not ScopeKind.ROOM:
            raise MemoryStateError(
                ReasonCode.POLICY_FORBIDDEN,
                f"{scope_kind.value} scope has no curated mapping in this slice",
            )
        snapshot = (
            self.core.get_record(
                runtime_origin.snapshot_ref.id, runtime_origin.snapshot_ref.revision
            )
            if runtime_origin.snapshot_ref is not None
            else None
        )
        if not isinstance(snapshot, ContextSnapshot):
            raise MemoryStateError(
                ReasonCode.INVALID_REFERENCE, "runtime origin cites a missing snapshot"
            )
        audience = self.core.get_record(snapshot.audience_ref.id, snapshot.audience_ref.revision)
        if not isinstance(audience, AudienceSnapshot) or audience.destination_id != scope_ref:
            raise MemoryStateError(
                ReasonCode.POLICY_FORBIDDEN,
                f"room scope must match the turn's destination ({scope_ref!r} does not)",
            )

    def _origin_coverage(self, runtime_origin: RuntimeOrigin) -> ProducerCoverage:
        if runtime_origin.producer_coverage_ref is None:
            raise MemoryStateError(
                ReasonCode.INVALID_REFERENCE, "runtime origin carries no producer coverage"
            )
        coverage = self.core.get_record(
            runtime_origin.producer_coverage_ref.id, runtime_origin.producer_coverage_ref.revision
        )
        if not isinstance(coverage, ProducerCoverage):
            raise MemoryStateError(
                ReasonCode.INVALID_REFERENCE, "runtime origin cites a missing producer coverage"
            )
        return coverage

    def _runtime_supplied_refs(self, runtime_origin: RuntimeOrigin) -> frozenset[RecordRef]:
        """The fragment refs the runtime actually put in front of the model:
        the producer coverage's input fragments plus every fragment an input
        view returned. Nothing else is citable lineage."""
        coverage = self._origin_coverage(runtime_origin)
        supplied = set(coverage.input_fragment_refs)
        for view_ref in coverage.input_view_refs:
            view = self.core.get_record(view_ref.id, view_ref.revision)
            if isinstance(view, MemoryView):
                supplied.update(view.returned_fragment_refs)
        return frozenset(supplied)

    def stage_memory(self, memory_ref: RecordRef, target_stage: MemoryStage) -> RecordRef:
        """Explicit staged promotion: EPHEMERAL → SOCIAL/CANDIDATE → DURABLE,
        always forward, never widening scope (scope is carried unchanged —
        no BROAD inference from DURABLE). Blocked with SUPERSEDED when the
        proposal cites a revision the memory has moved past."""
        memory = self._current_memory_or_error(memory_ref)
        if _STAGE_ORDER[target_stage] <= _STAGE_ORDER[memory.stage]:
            raise MemoryStateError(
                ReasonCode.POLICY_FORBIDDEN,
                f"promotion {memory.stage.value} -> {target_stage.value} does not advance",
            )
        if target_stage is MemoryStage.DURABLE and memory.stage is not MemoryStage.CANDIDATE:
            # §6.4: DURABLE promotion requires an existing CANDIDATE
            # revision — no skipping the curation stage (review).
            raise MemoryStateError(
                ReasonCode.POLICY_FORBIDDEN,
                f"DURABLE promotion requires a CANDIDATE memory, got {memory.stage.value}",
            )
        promoted = MemoryRecord(
            id=memory.id,
            revision=memory.revision + 1,
            created_at=self.clock.now_rfc3339(),
            assertion_fragment_ref=memory.assertion_fragment_ref,
            stage=target_stage,
            scope_kind=memory.scope_kind,
            scope_ref=memory.scope_ref,
            custodian_refs=memory.custodian_refs,
            subject_refs=memory.subject_refs,
            commitment_refs=memory.commitment_refs,
            assertion_status=memory.assertion_status,
            supersedes_ref=None,
            valid_until=memory.valid_until,
            status=MemoryStatus.ACTIVE,
        )
        self.core.promote_memory(
            promoted, expiry_ms=self._memory_expiry(memory.id, memory.revision)
        )
        return RecordRef(id=promoted.id, revision=promoted.revision)

    def correct_memory(self, memory_ref: RecordRef, corrected_text: str) -> RecordRef:
        """Correction: a new assertion revision supersedes the cited one
        (same stage/scope — corrections do not move scope either), and
        everything derived from the superseded assertion is invalidated via
        the dependency graph: derivations flip STALE, their retained output
        memories flip STALE (payload ineligible through the scope index),
        and affected snapshots flip INVALID. The whole correction — new
        assertion plus the invalidation walk — is ONE core transaction, so
        a crash mid-job (DURING_CORRECTION_JOB) rolls back to the
        pre-correction state and the correction simply re-runs: never a
        half-invalidated world where a queued effect's snapshot escaped
        invalidation."""
        memory = self._current_memory_or_error(memory_ref)
        now = self.clock.now_rfc3339()
        fragment_id = f"fragment:memory:{memory.id}:{memory.revision + 1}"
        written: list[Record] = []
        with self.core.transaction() as connection:
            self.core._put_payload_tx(
                connection, f"payload:{fragment_id}", corrected_text, owner_ref=memory.id
            )
            corrected_fragment = ContentFragment(
                id=fragment_id,
                revision=1,
                created_at=now,
                kind=FragmentKind.TEXT,
                payload_ref=f"payload:{fragment_id}",
                provenance=Provenance(
                    producer_ref=RecordRef(id="principal:agent", revision=1),
                    source_kind=SourceKind.MEMORY,
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
                    occurred_at=now,
                    observed_at=now,
                ),
                commitment_refs=memory.commitment_refs,
                eligibility_status=EligibilityStatus.ACTIVE,
            )
            self.core._put_record_tx(connection, corrected_fragment)
            written.append(corrected_fragment)
            corrected = MemoryRecord(
                id=memory.id,
                revision=memory.revision + 1,
                created_at=now,
                assertion_fragment_ref=RecordRef(id=fragment_id, revision=1),
                stage=memory.stage,
                scope_kind=memory.scope_kind,
                scope_ref=memory.scope_ref,
                custodian_refs=memory.custodian_refs,
                subject_refs=memory.subject_refs,
                commitment_refs=memory.commitment_refs,
                assertion_status=memory.assertion_status,
                supersedes_ref=RecordRef(id=memory.id, revision=memory.revision),
                valid_until=memory.valid_until,
                status=MemoryStatus.ACTIVE,
            )
            self.core._promote_memory_tx(
                connection, corrected, expiry_ms=self._memory_expiry(memory.id, memory.revision)
            )
            written.append(corrected)
            self._invalidate_dependents(
                connection,
                memory.assertion_fragment_ref.id,
                memory.assertion_fragment_ref.revision,
                now,
                written,
            )
            self.core._verify_ref_targets_tx(connection, tuple(written))
        return RecordRef(id=corrected.id, revision=corrected.revision)

    def revoke_view(self, view_ref: RecordRef) -> RecordRef:
        """I14: revoking a view invalidates every active snapshot that
        admitted it — revocation is not limited to retrieval-time
        eligibility, so a queued effect derived through the view can never
        pass revalidation on a stale admission. The dependency edges
        build_snapshot writes make the dependents findable; the snapshot's
        own active_view_refs decide the match (the version vector cites
        fragments, not views). The revocation, its cascade through
        transform-derived views, the stale-flip of retained output
        memories, and the snapshot invalidations are ONE core transaction,
        mirroring correct_memory: a mid-walk crash (DURING_CORRECTION_JOB
        — the invalidation-job boundary, tripped between the cascade and
        the invalidation phases) rolls every write back to the
        pre-revocation state and the revocation re-runs."""
        standing = self.core.current_ref(view_ref.id)
        if standing is None:
            raise MemoryStateError(
                ReasonCode.INVALID_REFERENCE, f"view {view_ref.id} does not exist"
            )
        view = self.core.get_record(view_ref.id, standing.revision)
        if not isinstance(view, MemoryView):
            raise MemoryStateError(
                ReasonCode.INVALID_REFERENCE, f"{view_ref.id} is not a memory view"
            )
        if view.status == RecordStatus.REVOKED:
            # Deliberately NOT a SUPERSEDED guard: views are born at
            # revision 1 and revocation is the only revision bump, so a
            # stale cite can only mean "already revoked" — returning the
            # standing revocation is exactly the crash-retry idempotence
            # this path needs (unlike memories, which correct_memory
            # guards because promotions move them forward).
            return standing
        now = self.clock.now_rfc3339()
        written: list[Record] = []
        with self.core.transaction() as connection:
            # the revocation cascades through transform-derived views (I14
            # late derivations): the request_access edge makes each output
            # view a dependent of its granted source, so withdrawing the
            # source withdraws the whole chain
            pending = [view]
            revoked_ids: set[str] = set()
            revoked_views: dict[str, MemoryView] = {}
            while pending:
                node = pending.pop()
                if node.id in revoked_ids:
                    continue
                revoked = replace(
                    node,
                    revision=node.revision + 1,
                    created_at=now,
                    status=RecordStatus.REVOKED,
                )
                self.core._put_record_tx(connection, revoked)
                written.append(revoked)
                revoked_ids.add(node.id)
                revoked_views[node.id] = node
                for child_id in self.core.children_of(node.id):
                    record = self.core.get_record(child_id)
                    if (
                        isinstance(record, MemoryView)
                        and record.status != RecordStatus.REVOKED
                        and record.id not in revoked_ids
                    ):
                        pending.append(record)
            # the invalidation-job crash boundary: writes above this line
            # (the revoked revisions) are what a crash must roll back
            self._trip(CrashCheckpoint.DURING_CORRECTION_JOB)
            invalidated: set[str] = set()
            for revoked_id, revoked_view in revoked_views.items():
                # retained output memories of what the views returned stop
                # being eligible: the scope index is exactly the "search
                # index" I14 says must not keep serving withdrawn data
                for fragment_ref in revoked_view.returned_fragment_refs:
                    self._stale_output_memory(connection, fragment_ref, now, written)
                for child_id in self.core.children_of(revoked_id):
                    record = self.core.get_record(child_id)
                    if not isinstance(record, ContextSnapshot):
                        continue
                    if record.id in invalidated:
                        continue
                    if not any(ref.id in revoked_ids for ref in record.active_view_refs):
                        continue
                    # no INVALID-status guard, matching _invalidate_from's
                    # snapshot branch: re-flipping an already-invalid
                    # snapshot only churns a revision and can never reopen
                    # dispatch
                    invalid = replace(
                        record,
                        revision=record.revision + 1,
                        created_at=now,
                        status=SnapshotStatus.INVALID,
                    )
                    self.core._put_record_tx(connection, invalid)
                    written.append(invalid)
                    invalidated.add(record.id)
            self.core._verify_ref_targets_tx(connection, tuple(written))
            root = written[0]
        return RecordRef(id=root.id, revision=root.revision)

    def _current_memory_or_error(self, memory_ref: RecordRef) -> MemoryRecord:
        current = self.core.current_ref(memory_ref.id)
        if current is None:
            raise MemoryStateError(
                ReasonCode.INVALID_REFERENCE, f"memory {memory_ref.id} does not exist"
            )
        if current.revision != memory_ref.revision:
            raise MemoryStateError(
                ReasonCode.SUPERSEDED,
                f"memory {memory_ref.id} is at revision {current.revision},"
                f" proposal cites {memory_ref.revision}",
            )
        record = self.core.get_record(memory_ref.id, memory_ref.revision)
        if not isinstance(record, MemoryRecord):
            raise MemoryStateError(ReasonCode.INVALID_REFERENCE, f"{memory_ref.id} is not a memory")
        if record.status != MemoryStatus.ACTIVE:
            # Invalidated (STALE) or removed (TOMBSTONED) memories cannot be
            # promoted or corrected back to relevance (review: invalidation
            # bypass through the module's own public API).
            raise MemoryStateError(
                ReasonCode.POLICY_FORBIDDEN,
                f"memory {memory_ref.id} is {record.status.value}: invalidated content"
                " cannot be promoted or corrected",
            )
        return record

    def _memory_expiry(self, memory_id: str, revision: int) -> int | None:
        rows = self.core.memory_scope_rows(memory_id)
        for row in rows:
            if row["revision"] == revision:
                return row["expiry_ms"]
        return None

    def _invalidate_dependents(
        self,
        connection: Any,
        fragment_id: str,
        fragment_revision: int,
        now: str,
        written: list[Record],
    ) -> None:
        """Corrections invalidate what was derived from the superseded
        assertion, transitively: a summary-of-a-summary chain propagates
        all the way down (source → summary A → summary B → snapshot), not
        one level (imi feedback on task 3). Only dependents whose captured
        version actually matches are affected; flipped revisions carry the
        correction time as their creation time. The walk runs inside the
        correction's one transaction — DURING_CORRECTION_JOB is the named
        crash boundary inside it."""
        self._trip(CrashCheckpoint.DURING_CORRECTION_JOB)
        self._invalidate_from(connection, fragment_id, fragment_revision, now, set(), written)

    def _invalidate_from(
        self,
        connection: Any,
        fragment_id: str,
        fragment_revision: int,
        now: str,
        visited: set[str],
        written: list[Record],
    ) -> None:
        if fragment_id in visited:
            return
        visited.add(fragment_id)
        for child_id in self.core.children_of(fragment_id):
            record = self.core.get_record(child_id)
            if isinstance(record, Derivation):
                if not _vector_cites(record.input_version_vector, fragment_id, fragment_revision):
                    continue
                stale = replace(
                    record,
                    revision=record.revision + 1,
                    created_at=now,
                    state=DerivationState.STALE,
                )
                self.core._put_record_tx(connection, stale)
                written.append(stale)
                self._stale_output_memory(connection, record.output_fragment_ref, now, written)
                # whatever was derived from THIS derivation's output is
                # built on stale content too — keep walking the graph
                self._invalidate_from(
                    connection,
                    record.output_fragment_ref.id,
                    record.output_fragment_ref.revision,
                    now,
                    visited,
                    written,
                )
            elif isinstance(record, ContextSnapshot):
                if not _vector_cites(record.version_vector, fragment_id, fragment_revision):
                    continue
                invalid = replace(
                    record,
                    revision=record.revision + 1,
                    created_at=now,
                    status=SnapshotStatus.INVALID,
                )
                self.core._put_record_tx(connection, invalid)
                written.append(invalid)

    def _stale_output_memory(
        self, connection: Any, output_fragment_ref: RecordRef, now: str, written: list[Record]
    ) -> None:
        """A derivation's retained output memory becomes ineligible: the
        scope-index row flips STALE with a new memory revision."""
        rows = self.core._conn.execute(
            "SELECT DISTINCT memory_id FROM core_memory_scope WHERE assertion_ref = ?",
            (output_fragment_ref.id,),
        ).fetchall()
        for row in rows:
            current = self.core.current_ref(row["memory_id"])
            record = self.core.get_record(row["memory_id"], current.revision) if current else None
            if not isinstance(record, MemoryRecord) or record.status == MemoryStatus.STALE:
                continue
            stale = replace(
                record,
                revision=record.revision + 1,
                created_at=now,
                status=MemoryStatus.STALE,
            )
            self.core._promote_memory_tx(
                connection, stale, expiry_ms=self._memory_expiry(record.id, record.revision)
            )
            written.append(stale)

    def build_snapshot(
        self,
        *,
        identity_ref: RecordRef,
        intent_ref: RecordRef,
        audience_ref: RecordRef,
        episode_ref: RecordRef | None,
        sink_ref: RecordRef,
        effect_capable: bool,
        call_id: str,
        active_view_refs: tuple[RecordRef, ...] = (),
    ) -> ContextSnapshot:
        """Assemble + persist a context snapshot with CONTEXT_ASSEMBLY
        coverage; the snapshot's restrictions derive from actual inputs.

        Admitted views are checked structurally at build time (task 3.3): a
        view whose permitted uses are internal-only — whether backed by a
        STRONG commitment or constructed so by a transform — can never enter
        an externally effect-capable snapshot, a DM included; a view is also
        bound to the audience and purpose it was granted for. The failure is
        here, never deferred to dispatch, and it is not reviewer-waivable."""
        audience = self.core.get_record(audience_ref.id, audience_ref.revision)
        intent = self.core.get_record(intent_ref.id, intent_ref.revision)
        admitted = self._admit_views(
            active_view_refs,
            effect_capable=effect_capable,
            audience=audience if isinstance(audience, AudienceSnapshot) else None,
            intent=intent if isinstance(intent, Intent) else None,
        )
        episode, used_bytes = (
            self.eligible_episode_fragments(episode_ref) if episode_ref else ((), 0)
        )
        memories = (
            self._eligible_memory_fragments(audience.destination_id, MAX_CONTEXT_BYTES - used_bytes)
            if isinstance(audience, AudienceSnapshot)
            else ()
        )
        selected = tuple(dict.fromkeys((*episode, *memories)))
        inherited = tuple(
            dict.fromkeys(ref for view in admitted for ref in view.inherited_commitment_refs)
        )
        admitted_refs = tuple(RecordRef(id=view.id, revision=view.revision) for view in admitted)
        coverage = ProducerCoverage(
            id=f"coverage:context:{call_id}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            producer_kind=ProducerKind.CONTEXT_ASSEMBLY,
            input_fragment_refs=selected,
            input_view_refs=admitted_refs,
            input_intent_refs=(intent_ref,),
            input_policy_refs=(),
            sink_ref=sink_ref,
            call_id=call_id,
            runtime_epoch=self.core.runtime_epoch,
        )
        snapshot = ContextSnapshot(
            id=f"snapshot:{call_id}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            identity_ref=identity_ref,
            intent_ref=intent_ref,
            audience_ref=audience_ref,
            episode_ref=episode_ref,
            selected_fragment_refs=selected,
            active_view_refs=admitted_refs,
            producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
            exposure_class=ExposureClass.ORDINARY,
            inherited_commitment_refs=inherited,
            version_vector=tuple(sorted(selected, key=lambda ref: ref.id)),
            sink_ref=sink_ref,
            effect_capable=effect_capable,
            serializer_version=self.serializer_version,
            status=SnapshotStatus.ACTIVE,
        )
        # the snapshot, its coverage, and the dependency edges the
        # invalidation walks rely on commit as ONE unit: a crash can never
        # leave a durable snapshot with missing edges (hermes-agora-5e2)
        with self.core.transaction() as connection:
            self.core._put_record_tx(connection, coverage)
            self.core._put_record_tx(connection, snapshot)
            self.core._verify_ref_targets_tx(connection, (coverage, snapshot))
            snapshot_ref = RecordRef(id=snapshot.id, revision=snapshot.revision)
            for ref in (*selected, *admitted_refs):
                self.core._add_dependency_tx(
                    connection, snapshot_ref, ref, DependencyKind.PRODUCER_INPUT
                )
        return snapshot

    def _admit_views(
        self,
        active_view_refs: tuple[RecordRef, ...],
        *,
        effect_capable: bool,
        audience: AudienceSnapshot | None,
        intent: Intent | None,
    ) -> tuple[MemoryView, ...]:
        """Load and structurally admit the requested views into THIS
        context. Failures: a missing view, a non-current citation, a
        non-ACTIVE status, or an expired valid_until → STALE_CONTEXT; an
        internal-only view against an externally effect-capable snapshot,
        an audience the view's bounds do not cover, or an intent whose
        purpose class the granting authorizations no longer cover →
        VIEW_FORBIDDEN (task 3.3 + imi feedback: a view is bound to the
        context it was granted for — it cannot be carried into another
        room, a DM, or a different purpose because its bytes exist)."""
        admitted: list[MemoryView] = []
        for view_ref in active_view_refs:
            record = self.core.get_record(view_ref.id, view_ref.revision)
            if not isinstance(record, MemoryView):
                raise ViewAdmissionError(
                    ReasonCode.STALE_CONTEXT, f"view {view_ref.id} does not resolve"
                )
            current = self.core.current_ref(view_ref.id)
            if current is None or current.revision != view_ref.revision:
                raise ViewAdmissionError(
                    ReasonCode.STALE_CONTEXT,
                    f"view {view_ref.id} cited at revision {view_ref.revision} is not current",
                )
            if record.status != RecordStatus.ACTIVE:
                raise ViewAdmissionError(
                    ReasonCode.STALE_CONTEXT, f"view {view_ref.id} is not active"
                )
            now = self.clock.now_rfc3339()
            if record.valid_until is not None and record.valid_until < now:
                raise ViewAdmissionError(
                    ReasonCode.STALE_CONTEXT, f"view {view_ref.id} has expired"
                )
            if effect_capable and all(
                use == IntendedUse.INTERNAL_ONLY for use in record.permitted_uses
            ):
                raise ViewAdmissionError(
                    ReasonCode.VIEW_FORBIDDEN,
                    f"strongly internal-only view {view_ref.id} cannot enter an"
                    " externally effect-capable snapshot (including a DM)",
                )
            if audience is not None and audience.destination_id not in record.audience_bounds:
                raise ViewAdmissionError(
                    ReasonCode.VIEW_FORBIDDEN,
                    f"view {view_ref.id} is not valid for audience {audience.destination_id}",
                )
            if intent is not None and record.authorization_refs:
                for auth_ref in record.authorization_refs:
                    authorization = self.core.get_record(auth_ref.id, auth_ref.revision)
                    if isinstance(authorization, UseAuthorization) and (
                        intent.purpose_class not in authorization.permitted_purpose_classes
                    ):
                        raise ViewAdmissionError(
                            ReasonCode.VIEW_FORBIDDEN,
                            f"view {view_ref.id} was granted for purpose class"
                            f" {authorization.permitted_purpose_classes}; the"
                            f" entering intent is {intent.purpose_class!r}",
                        )
            admitted.append(record)
        return tuple(admitted)

    def record_envelope(
        self,
        *,
        snapshot: ContextSnapshot,
        purpose: EnvelopePurpose,
        messages: list[dict[str, Any]],
        sink_ref: RecordRef,
        call_id: str,
    ) -> ModelCallEnvelope:
        """Persist the exact model-call envelope: the messages payload goes
        out-of-line; the envelope binds its digest and supplied refs."""
        payload_bytes = canonical_json_bytes(messages)
        payload_id = f"payload:messages:{call_id}"
        self.core.put_payload(
            payload_id,
            payload_bytes.decode("utf-8"),
            owner_ref=f"snapshot:{snapshot.id}",
        )
        envelope = ModelCallEnvelope(
            id=f"envelope:{call_id}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            snapshot_ref=RecordRef(id=snapshot.id, revision=snapshot.revision),
            purpose=purpose,
            exact_messages_payload_ref=payload_id,
            tool_schema_digest=_digest(b"no-tools:v1"),
            supplied_fragment_refs=snapshot.selected_fragment_refs,
            supplied_view_refs=snapshot.active_view_refs,
            sink_ref=sink_ref,
            request_digest=_digest(payload_bytes),
        )
        self.core.put_record(envelope)
        return envelope

    def record_step_coverage(
        self,
        *,
        snapshot: ContextSnapshot,
        envelope: ModelCallEnvelope,
        call_id: str,
    ) -> ProducerCoverage:
        """Every model output gets runtime producer coverage from the actual
        invocation inputs (handoff §4.3 / D05)."""
        coverage = ProducerCoverage(
            id=f"coverage:main:{call_id}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            producer_kind=ProducerKind.MAIN,
            input_fragment_refs=snapshot.selected_fragment_refs,
            input_view_refs=snapshot.active_view_refs,
            input_intent_refs=(snapshot.intent_ref,),
            input_policy_refs=(),
            sink_ref=envelope.sink_ref,
            call_id=call_id,
            runtime_epoch=self.core.runtime_epoch,
        )
        self.core.put_record(coverage)
        self.core.add_dependency(
            RecordRef(id=coverage.id, revision=coverage.revision),
            RecordRef(
                id=snapshot.producer_coverage_ref.id,
                revision=snapshot.producer_coverage_ref.revision,
            ),
            DependencyKind.PRODUCER_INPUT,
        )
        return coverage


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
