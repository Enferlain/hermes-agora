"""Context and memory assembly for the M1 slice (handoff §2, §5).

ContextMemory builds audience/scoped context snapshots with runtime
producer coverage, records model-call envelopes with out-of-line message
payloads, and persists step coverage for every accepted model output. It
owns no sending and no agent personality. Eligibility here is the M1
mechanical slice (recent episode fragments within the fixture bounds); M2
adds scoped memory, views, and discretion.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agora.contracts import (
    ContextSnapshot,
    DependencyKind,
    EnvelopePurpose,
    ExposureClass,
    ModelCallEnvelope,
    ProducerCoverage,
    ProducerKind,
    RecordRef,
    SnapshotStatus,
    canonical_json_bytes,
)
from agora.ports import CrashCheckpoint, FaultInjector, VirtualClock
from agora.stores import CoreStore

MAX_SELECTED_EVENTS = 20
MAX_CONTEXT_BYTES = 16 * 1024


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

    def eligible_episode_fragments(self, episode_ref: RecordRef) -> tuple[RecordRef, ...]:
        """Recent ingested fragments for an episode, newest last, within the
        fixture bounds (last 20 events, 16 KiB, whole-fragment omission)."""
        rows = self.core._conn.execute(
            "SELECT fragment_refs FROM core_ingestion ORDER BY journal_seq DESC LIMIT ?",
            (MAX_SELECTED_EVENTS,),
        ).fetchall()
        selected: list[RecordRef] = []
        total = 0
        for row in rows:
            for fragment_id in json.loads(row["fragment_refs"]):
                ref = self.core.current_ref(fragment_id)
                if ref is None:
                    continue
                payload_row = self.core._conn.execute(
                    "SELECT length(data) AS n FROM core_payloads WHERE payload_id = ?",
                    (f"payload:fragment:{fragment_id}",),
                ).fetchone()
                size = payload_row["n"] if payload_row is not None else 0
                if total + size > MAX_CONTEXT_BYTES:
                    continue  # omit excess whole fragments, keep metadata
                total += size
                selected.append(ref)
        return tuple(reversed(selected))

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
    ) -> ContextSnapshot:
        """Assemble + persist a context snapshot with CONTEXT_ASSEMBLY
        coverage; the snapshot's restrictions derive from actual inputs."""
        selected = self.eligible_episode_fragments(episode_ref) if episode_ref else ()
        coverage = ProducerCoverage(
            id=f"coverage:context:{call_id}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            producer_kind=ProducerKind.CONTEXT_ASSEMBLY,
            input_fragment_refs=selected,
            input_view_refs=(),
            input_intent_refs=(intent_ref,),
            input_policy_refs=(),
            sink_ref=sink_ref,
            call_id=call_id,
            runtime_epoch=self.core.runtime_epoch,
        )
        self.core.put_record(coverage)
        snapshot = ContextSnapshot(
            id=f"snapshot:{call_id}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            identity_ref=identity_ref,
            intent_ref=intent_ref,
            audience_ref=audience_ref,
            episode_ref=episode_ref,
            selected_fragment_refs=selected,
            active_view_refs=(),
            producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
            exposure_class=ExposureClass.ORDINARY,
            inherited_commitment_refs=(),
            version_vector=tuple(sorted(selected, key=lambda ref: ref.id)),
            sink_ref=sink_ref,
            effect_capable=effect_capable,
            serializer_version=self.serializer_version,
            status=SnapshotStatus.ACTIVE,
        )
        self.core.put_record(snapshot)
        for ref in selected:
            self.core.add_dependency(
                RecordRef(id=snapshot.id, revision=snapshot.revision),
                ref,
                DependencyKind.PRODUCER_INPUT,
            )
        return snapshot

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
