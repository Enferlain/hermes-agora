"""Replay runner: fixture manifest, serialized event/turn loop, trace output.

The manifest is fixture data through the authenticated control path; its
scripted steps are raw payloads parsed through the model boundary
(``parse_model_step``), so runtime-only field discipline applies to
fixtures exactly as it would to a model adapter. The runner derives no
domain policy from filenames or text (handoff §2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agora.commit_gate import CommitGate
from agora.context_memory import ContextMemory
from agora.contracts import (
    AgentIdentity,
    AssertionStatus,
    AudienceSnapshot,
    CapabilityEnvelope,
    ContentFragment,
    EligibilityStatus,
    FragmentKind,
    InstructionAuthority,
    Integrity,
    IntentState,
    Provenance,
    RecordRef,
    SourceKind,
    parse_model_step,
)
from agora.coordinator import Coordinator
from agora.fake_sidecar import FakeSidecar, FakeWorld
from agora.ports import FaultInjector, ScriptedMainModel, VirtualClock
from agora.stores import CoreStore, JournalEntry, TransportStore

MANIFEST_VERSION = 1


class ManifestError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class FixtureEvent:
    at_ms: int
    event_id: str
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class RunManifest:
    fixture_id: str
    clock_anchor: str
    destination_id: str
    allowed_destinations: tuple[str, ...]
    events: tuple[FixtureEvent, ...]
    steps: tuple[Any, ...]  # parsed MainStep objects, consumed in order

    @classmethod
    def from_payload(cls, data: object) -> RunManifest:
        if not isinstance(data, dict):
            raise ManifestError("manifest must be an object")
        required = {
            "manifest_version",
            "fixture_id",
            "clock_anchor",
            "destination_id",
            "allowed_destinations",
            "events",
            "steps",
        }
        if set(data) != required:
            raise ManifestError(
                f"manifest fields must be exactly {sorted(required)}; got {sorted(data)}"
            )
        if data["manifest_version"] != MANIFEST_VERSION:
            raise ManifestError(f"unsupported manifest version {data['manifest_version']!r}")
        events = tuple(
            FixtureEvent(
                at_ms=int(event["at_ms"]),
                event_id=str(event["event_id"]),
                kind=str(event["kind"]),
                text=str(event["text"]),
            )
            for event in data["events"]
        )
        steps = tuple(parse_model_step(step) for step in data["steps"])
        return cls(
            fixture_id=str(data["fixture_id"]),
            clock_anchor=str(data["clock_anchor"]),
            destination_id=str(data["destination_id"]),
            allowed_destinations=tuple(str(item) for item in data["allowed_destinations"]),
            events=events,
            steps=steps,
        )


class ReplayRunner:
    """Deterministic replay over the full stack; produces an observer trace."""

    def __init__(self, manifest: RunManifest, run_dir: Path) -> None:
        self.manifest = manifest
        self.run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        self.clock = VirtualClock(manifest.clock_anchor)
        self.core = CoreStore(run_dir / "core.sqlite")
        self.transport = TransportStore(run_dir / "transport.sqlite")
        self.world = FakeWorld(run_dir / "world.sqlite")
        self.injector = FaultInjector()
        self._wire()

    def _wire(self, steps: tuple[Any, ...] | None = None) -> None:
        if steps is None:
            steps = self.manifest.steps
        known = self.core.get_record("identity:agent")
        if known is None:
            identity, audience, capability, sink_ref = Coordinator.bootstrap(
                self.core,
                self.clock,
                destination_id=self.manifest.destination_id,
                allowed_destination_ids=self.manifest.allowed_destinations,
            )
        else:
            assert isinstance(known, AgentIdentity)
            audience = self.core.get_record(f"audience:{self.manifest.destination_id}")
            capability = self.core.get_record("capability:social")
            sink_ref = RecordRef(id="sink:scripted", revision=1)
            assert isinstance(audience, AudienceSnapshot)
            assert isinstance(capability, CapabilityEnvelope)
            identity, _, _ = known, audience, capability
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

    def run(self) -> dict[str, Any]:
        trace: dict[str, Any] = {
            "fixture": self.manifest.fixture_id,
            "ingested": [],
            "turns": [],
            "world": [],
        }
        for index, event in enumerate(sorted(self.manifest.events, key=lambda e: e.at_ms), start=1):
            self.clock.advance_to(event.at_ms)
            self._append_event(index, event)
            receipt = self.coordinator.ingest(self.transport, consumer_id="core-1")
            trace["ingested"].append(
                {"seq": index, "event": event.event_id, "applied": list(receipt.applied)}
            )
        self._drain(trace)
        self._fire_due_timers(trace)
        self._drain(trace)
        trace["world"] = self._world_rows()
        return trace

    def _append_event(self, seq: int, event: FixtureEvent) -> None:
        self.transport.append_events(
            (
                JournalEntry(
                    journal_seq=seq,
                    source_event_id=event.event_id,
                    source_revision=1,
                    kind=event.kind,
                    payload_ref=f"payload:{event.event_id}",
                    fragment_refs=(f"fragment:{event.event_id}",),
                    recovery="COMPLETE",
                ),
            )
        )
        self.core.put_payload(f"payload:{event.event_id}", event.text, owner_ref=event.event_id)
        fragment = ContentFragment(
            id=f"fragment:{event.event_id}",
            revision=1,
            created_at=self.clock.now_rfc3339(),
            kind=FragmentKind.TEXT,
            payload_ref=f"payload:{event.event_id}",
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
                occurred_at=self.clock.now_rfc3339(),
                observed_at=self.clock.now_rfc3339(),
            ),
            commitment_refs=(),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
        self.core.put_record(fragment)

    def _drain(self, trace: dict[str, Any]) -> None:
        for work_id in self.coordinator.ready_work_ids():
            result = self.coordinator.run_turn(work_id)
            trace["turns"].append(
                {
                    "work": work_id,
                    "outcome": result.outcome.kind.value,
                    "reasons": [code.value for code in result.outcome.reason_codes],
                    "committed": result.receipt_ref is not None,
                }
            )

    def _fire_due_timers(self, trace: dict[str, Any]) -> None:
        """Deferred intents wake exactly once: a deduped TIMER_DUE occurrence
        at the recorded due time (handoff §6.4 fixed-time defer)."""
        rows = self.core._conn.execute(
            "SELECT record_id, payload_json FROM core_records"
            " WHERE record_type = 'Intent' AND is_current = 1"
        ).fetchall()
        next_seq = self.transport.max_journal_seq + 1
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("state") != IntentState.DEFERRED.value:
                continue
            due_at = payload.get("due_at")
            if not due_at:
                continue
            due_ms = self._rfc3339_to_ms(due_at)
            self.clock.advance_to(due_ms)
            timer_event_id = f"timer:{row['record_id']}:{due_at}:{payload['endorsement_revision']}"
            self.transport.append_events(
                (
                    JournalEntry(
                        journal_seq=next_seq,
                        source_event_id=timer_event_id,
                        source_revision=1,
                        kind="TIMER_DUE",
                        payload_ref=f"payload:{timer_event_id}",
                        fragment_refs=(),
                        recovery="COMPLETE",
                    ),
                )
            )
            next_seq += 1
            receipt = self.coordinator.ingest(self.transport, consumer_id="core-1")
            trace["ingested"].append(
                {"seq": next_seq - 1, "event": timer_event_id, "applied": list(receipt.applied)}
            )

    def _world_rows(self) -> list[dict[str, Any]]:
        rows = self.world._conn.execute(
            "SELECT attempt_id, effect_id, payload_bytes, visible_at_ms FROM world_effects"
            " ORDER BY visible_at_ms"
        ).fetchall()
        return [dict(row) for row in rows]

    def _rfc3339_to_ms(self, value: str) -> int:
        from datetime import datetime

        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        anchor = datetime.fromisoformat(self.manifest.clock_anchor.replace("Z", "+00:00"))
        return int((moment - anchor).total_seconds() * 1000)
