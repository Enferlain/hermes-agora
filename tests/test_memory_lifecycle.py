"""M2 task 3.1: scoped memory eligibility via core.memory_scope.

The scope index is written transactionally with record promotion;
selection filters by stage/scope/status/expiry on the index before any
payload read; there is no global retrieval index.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.context_memory import ContextMemory
from agora.contracts import (
    AssertionStatus,
    ContentFragment,
    ContextSnapshot,
    EligibilityStatus,
    FragmentKind,
    InstructionAuthority,
    Integrity,
    MemoryRecord,
    MemoryStage,
    MemoryStatus,
    Principal,
    PrincipalKind,
    ProducerCoverage,
    Provenance,
    RecordRef,
    ScopeKind,
    SourceKind,
)
from agora.stores import CoreStore, InvalidReferenceError
from tests.fixtures_m2 import ANCHOR

NOW_MS = 0
EXPIRY_MS = 10_000_000


def seed_principal(core: CoreStore) -> None:
    if core.get_record("principal:agent") is not None:
        return  # the runner bootstrap already created it (different aliases)
    core.put_record(
        Principal(
            id="principal:agent",
            revision=1,
            created_at=ANCHOR,
            kind=PrincipalKind.AGENT,
            verified_bindings=(),
            claimed_aliases=(),
        )
    )


def seed_memory(
    core: CoreStore,
    memory_id: str,
    *,
    scope_kind: ScopeKind = ScopeKind.ROOM,
    scope_ref: str = "room:a",
    stage: MemoryStage = MemoryStage.SOCIAL,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    text: str = "ordinary remembered fact",
    expiry_ms: int | None = EXPIRY_MS,
    revision: int = 1,
) -> RecordRef:
    seed_principal(core)
    fragment_id = f"fragment:memory:{memory_id}"
    core.put_payload(f"payload:{fragment_id}", text, owner_ref=memory_id)
    core.put_record(
        ContentFragment(
            id=fragment_id,
            revision=1,
            created_at=ANCHOR,
            kind=FragmentKind.TEXT,
            payload_ref=f"payload:{fragment_id}",
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
                occurred_at=ANCHOR,
                observed_at=ANCHOR,
            ),
            commitment_refs=(),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
    )
    memory = MemoryRecord(
        id=memory_id,
        revision=revision,
        created_at=ANCHOR,
        assertion_fragment_ref=RecordRef(id=fragment_id, revision=1),
        stage=stage,
        scope_kind=scope_kind,
        scope_ref=scope_ref,
        custodian_refs=(),
        subject_refs=(),
        commitment_refs=(),
        assertion_status=AssertionStatus.OBSERVED_STATEMENT,
        supersedes_ref=None,
        valid_until=None,
        status=status,
    )
    core.promote_memory(memory, expiry_ms=expiry_ms)
    return RecordRef(id=memory_id, revision=revision)


def test_scope_index_written_transactionally_with_promotion(tmp_path: Path) -> None:
    core = CoreStore(tmp_path / "core.sqlite")
    ref = seed_memory(core, "memory:a")
    rows = core.memory_scope_rows("memory:a")
    assert len(rows) == 1
    assert rows[0]["stage"] == "SOCIAL"
    assert rows[0]["scope_kind"] == "ROOM"
    assert rows[0]["scope_ref"] == "room:a"
    assert rows[0]["assertion_ref"] == "fragment:memory:memory:a"
    assert core.get_record(ref.id, ref.revision) is not None


def test_failed_promotion_writes_neither_record_nor_index(tmp_path: Path) -> None:
    core = CoreStore(tmp_path / "core.sqlite")
    memory = MemoryRecord(
        id="memory:bad",
        revision=1,
        created_at=ANCHOR,
        assertion_fragment_ref=RecordRef(id="fragment:missing", revision=1),
        stage=MemoryStage.SOCIAL,
        scope_kind=ScopeKind.ROOM,
        scope_ref="room:a",
        custodian_refs=(),
        subject_refs=(),
        commitment_refs=(),
        assertion_status=AssertionStatus.OBSERVED_STATEMENT,
        supersedes_ref=None,
        valid_until=None,
        status=MemoryStatus.ACTIVE,
    )
    try:
        core.promote_memory(memory, expiry_ms=EXPIRY_MS)
        raise AssertionError("expected InvalidReferenceError")
    except InvalidReferenceError:
        pass
    assert core.get_record("memory:bad") is None
    assert core.memory_scope_rows("memory:bad") == []


def test_agent_scoped_memory_eligible_across_contexts(tmp_path: Path) -> None:
    core = CoreStore(tmp_path / "core.sqlite")
    seed_memory(core, "memory:own", scope_kind=ScopeKind.AGENT, scope_ref="agent")
    in_a = core.eligible_memory_refs(
        scope_kind=ScopeKind.ROOM.value, scope_ref="room:a", now_ms=NOW_MS
    )
    in_b = core.eligible_memory_refs(
        scope_kind=ScopeKind.ROOM.value, scope_ref="room:b", now_ms=NOW_MS
    )
    assert [ref.id for ref in in_a] == ["memory:own"]
    assert [ref.id for ref in in_b] == ["memory:own"]


def test_room_scoped_memory_matches_only_its_own_room(tmp_path: Path) -> None:
    core = CoreStore(tmp_path / "core.sqlite")
    seed_memory(core, "memory:a", scope_ref="room:a")
    seed_memory(core, "memory:b", scope_ref="room:b")
    selected = core.eligible_memory_refs(
        scope_kind=ScopeKind.ROOM.value, scope_ref="room:a", now_ms=NOW_MS
    )
    assert [ref.id for ref in selected] == ["memory:a"]
    # room membership or merges confer no scope rights: room:b's memory
    # payload is never even a candidate in room:a


def test_stale_expired_and_stage_filtered_memories_ineligible(tmp_path: Path) -> None:
    core = CoreStore(tmp_path / "core.sqlite")
    seed_memory(core, "memory:stale", status=MemoryStatus.STALE)
    seed_memory(core, "memory:expired", expiry_ms=5)
    seed_memory(core, "memory:ephemeral", stage=MemoryStage.EPHEMERAL)
    seed_memory(core, "memory:tombstoned", status=MemoryStatus.TOMBSTONED)
    selected = core.eligible_memory_refs(
        scope_kind=ScopeKind.ROOM.value,
        scope_ref="room:a",
        now_ms=100,  # past the 5ms expiry
        stages=frozenset({MemoryStage.SOCIAL, MemoryStage.CANDIDATE, MemoryStage.DURABLE}),
    )
    assert selected == ()


def test_promotion_to_new_revision_supersedes_old_scope_row(tmp_path: Path) -> None:
    core = CoreStore(tmp_path / "core.sqlite")
    seed_memory(core, "memory:promo", stage=MemoryStage.SOCIAL)
    seed_memory(
        core,
        "memory:promo",
        stage=MemoryStage.DURABLE,
        revision=2,
    )
    rows = core.memory_scope_rows("memory:promo")
    assert [row["revision"] for row in rows] == [1, 2]
    selected = core.eligible_memory_refs(
        scope_kind=ScopeKind.ROOM.value, scope_ref="room:a", now_ms=NOW_MS
    )
    # only the current revision is eligible, never the superseded row
    assert [(ref.id, ref.revision) for ref in selected] == [("memory:promo", 2)]


def test_snapshot_includes_eligible_memory_fragments(tmp_path: Path) -> None:
    from tests.test_request_access import boot

    core, context, _runner, intent_ref = boot(tmp_path)
    seed_memory(
        core,
        "memory:room",
        scope_ref="room:public",
        text="remembered public-room fact",
    )
    seed_memory(
        core,
        "memory:other-room",
        scope_ref="room:elsewhere",
        text="fact from another room",
    )
    snapshot = context.build_snapshot(
        identity_ref=RecordRef(id="identity:agent", revision=1),
        intent_ref=intent_ref,
        audience_ref=RecordRef(id="audience:room:public", revision=1),
        episode_ref=None,
        sink_ref=RecordRef(id="sink:scripted", revision=1),
        effect_capable=False,
        call_id="snapshot:mem-test",
    )
    fragment_ids = [ref.id for ref in snapshot.selected_fragment_refs]
    assert "fragment:memory:memory:room" in fragment_ids
    assert "fragment:memory:memory:other-room" not in fragment_ids
    # coverage and the version vector carry exactly the selected fragments:
    # the other room's payload never becomes a producer input
    coverage = core.get_record(snapshot.producer_coverage_ref.id)
    assert isinstance(coverage, ProducerCoverage)
    covered = [ref.id for ref in coverage.input_fragment_refs]
    assert "fragment:memory:memory:room" in covered
    assert "fragment:memory:memory:other-room" not in covered


# ---------------------------------------------------------------------------
# Task 3.2: staged promotion and corrections
# ---------------------------------------------------------------------------


def test_promotion_never_widens_scope(tmp_path: Path) -> None:
    from agora.stores import CoreStore as Store

    core = Store(tmp_path / "core.sqlite")
    context = _context_for(core)
    seed_memory(core, "memory:cand", scope_ref="room:a", stage=MemoryStage.CANDIDATE)
    promoted = context.stage_memory(RecordRef(id="memory:cand", revision=1), MemoryStage.DURABLE)
    assert promoted.revision == 2
    rows = core.memory_scope_rows("memory:cand")
    assert [row["revision"] for row in rows] == [1, 2]
    assert rows[1]["stage"] == "DURABLE"
    assert rows[1]["scope_kind"] == "ROOM" and rows[1]["scope_ref"] == "room:a"
    # DURABLE in room:a does NOT confer eligibility elsewhere
    assert [
        ref.id
        for ref in core.eligible_memory_refs(
            scope_kind=ScopeKind.ROOM.value, scope_ref="room:a", now_ms=NOW_MS
        )
    ] == ["memory:cand"]
    assert (
        core.eligible_memory_refs(
            scope_kind=ScopeKind.ROOM.value, scope_ref="room:other", now_ms=NOW_MS
        )
        == ()
    )
    # agent-scoped ordinary memory stays cross-context after promotion
    seed_memory(core, "memory:own", scope_kind=ScopeKind.AGENT, scope_ref="agent")
    context.stage_memory(RecordRef(id="memory:own", revision=1), MemoryStage.CANDIDATE)
    both = core.eligible_memory_refs(
        scope_kind=ScopeKind.ROOM.value, scope_ref="room:anywhere", now_ms=NOW_MS
    )
    assert [ref.id for ref in both] == ["memory:own"]


def test_non_forward_promotion_rejected(tmp_path: Path) -> None:
    from agora.context_memory import MemoryStateError
    from agora.stores import CoreStore as Store

    core = Store(tmp_path / "core.sqlite")
    context = _context_for(core)
    seed_memory(core, "memory:s", stage=MemoryStage.SOCIAL)
    with pytest.raises(MemoryStateError):
        context.stage_memory(RecordRef(id="memory:s", revision=1), MemoryStage.SOCIAL)
    with pytest.raises(MemoryStateError):
        context.stage_memory(RecordRef(id="memory:s", revision=1), MemoryStage.EPHEMERAL)


def test_stale_promotion_blocked_on_version_mismatch(tmp_path: Path) -> None:
    from agora.context_memory import MemoryStateError
    from agora.stores import CoreStore as Store

    core = Store(tmp_path / "core.sqlite")
    context = _context_for(core)
    seed_memory(core, "memory:src")
    context.correct_memory(RecordRef(id="memory:src", revision=1), "corrected fact")
    # a promotion proposal citing the superseded revision is stale
    with pytest.raises(MemoryStateError):
        context.stage_memory(RecordRef(id="memory:src", revision=1), MemoryStage.DURABLE)


def test_correction_supersedes_and_invalidates_derivations(tmp_path: Path) -> None:
    from agora.contracts import DependencyKind, Derivation, DerivationState, ProducerKind

    core = CoreStore(tmp_path / "core.sqlite")
    context = _context_for(core)
    seed_memory(core, "memory:src", text="original fact")

    # a summary derived from the source assertion, retained as a memory
    # (seed_memory creates fragment:memory:memory:summary + the record)
    seed_memory(core, "memory:summary", text="summary of original", stage=MemoryStage.CANDIDATE)
    summary_fragment_id = "fragment:memory:memory:summary"
    coverage = ProducerCoverage(
        id="coverage:summary",
        revision=1,
        created_at=ANCHOR,
        producer_kind=ProducerKind.SUMMARY,
        input_fragment_refs=(RecordRef(id="fragment:memory:memory:src", revision=1),),
        input_view_refs=(),
        input_intent_refs=(),
        input_policy_refs=(),
        sink_ref=None,
        call_id="summary:1",
        runtime_epoch=core.runtime_epoch,
    )
    core.put_record(coverage)
    core.put_record(
        Derivation(
            id="derivation:summary",
            revision=1,
            created_at=ANCHOR,
            output_fragment_ref=RecordRef(id=summary_fragment_id, revision=1),
            producer_coverage_ref=RecordRef(id=coverage.id, revision=1),
            claimed_parent_refs=(RecordRef(id="fragment:memory:memory:src", revision=1),),
            transform_id=None,
            authorization_refs=(),
            inherited_commitment_refs=(),
            input_version_vector=(RecordRef(id="fragment:memory:memory:src", revision=1),),
            state=DerivationState.CURRENT,
        )
    )
    core.add_dependency(
        RecordRef(id="derivation:summary", revision=1),
        RecordRef(id="fragment:memory:memory:src", revision=1),
        DependencyKind.PRODUCER_INPUT,
    )
    seed_memory(
        core,
        "memory:summary",
        text="summary of original",
        stage=MemoryStage.CANDIDATE,
    )

    corrected = context.correct_memory(RecordRef(id="memory:src", revision=1), "corrected fact")
    assert corrected.revision == 2
    source_memory = core.get_record("memory:src")
    assert isinstance(source_memory, MemoryRecord)
    assert source_memory.supersedes_ref == RecordRef(id="memory:src", revision=1)

    derivation = core.get_record("derivation:summary")
    assert isinstance(derivation, Derivation)
    assert derivation.state == DerivationState.STALE  # stale summary after correction
    summary_memory = core.get_record("memory:summary")
    assert isinstance(summary_memory, MemoryRecord)
    assert summary_memory.status == MemoryStatus.STALE  # payload ineligible
    selected = core.eligible_memory_refs(
        scope_kind=ScopeKind.ROOM.value, scope_ref="room:a", now_ms=NOW_MS
    )
    assert "memory:summary" not in [ref.id for ref in selected]
    assert "memory:src" in [ref.id for ref in selected]  # the correction itself


def test_correction_invalidates_snapshots_built_on_the_old_assertion(tmp_path: Path) -> None:
    from tests.test_request_access import boot

    core, context, _runner, intent_ref = boot(tmp_path)
    seed_memory(core, "memory:room", scope_ref="room:public", text="original room fact")
    snapshot = context.build_snapshot(
        identity_ref=RecordRef(id="identity:agent", revision=1),
        intent_ref=intent_ref,
        audience_ref=RecordRef(id="audience:room:public", revision=1),
        episode_ref=None,
        sink_ref=RecordRef(id="sink:scripted", revision=1),
        effect_capable=False,
        call_id="snapshot:before-correction",
    )
    assert snapshot.status.value == "ACTIVE"
    context.correct_memory(RecordRef(id="memory:room", revision=1), "corrected room fact")
    invalidated = core.get_record(snapshot.id)
    assert isinstance(invalidated, ContextSnapshot)
    assert invalidated.status.value == "INVALID"


def _plain_provenance() -> Provenance:
    return Provenance(
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
        occurred_at=ANCHOR,
        observed_at=ANCHOR,
    )


def _context_for(core: CoreStore) -> ContextMemory:
    from agora.ports import VirtualClock

    return ContextMemory(core, VirtualClock(ANCHOR))


def _plain_provenance_for(producer_id: str) -> Provenance:
    return Provenance(
        producer_ref=RecordRef(id=producer_id, revision=1),
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
        occurred_at=ANCHOR,
        observed_at=ANCHOR,
    )


# ---------------------------------------------------------------------------
# Review(section 3) regressions
# ---------------------------------------------------------------------------


def test_context_budget_is_shared_across_episodes_and_memories(tmp_path: Path) -> None:
    from agora.context_memory import MAX_CONTEXT_BYTES
    from agora.ports import VirtualClock
    from agora.stores import CoreStore as Store

    core = Store(tmp_path / "core.sqlite")
    clock = VirtualClock(ANCHOR)
    context = ContextMemory(core, clock)
    seed_memory(core, "memory:big", scope_ref="room:a", text="x" * (MAX_CONTEXT_BYTES - 100))
    # an episode fragment consuming most of the budget leaves the memory
    # over-budget and omitted: one 16 KiB limit, not two
    big_fragment = "fragment:evt:big"
    core.put_payload(f"payload:{big_fragment}", "y" * (MAX_CONTEXT_BYTES - 100), owner_ref="evt")
    core.put_record(
        ContentFragment(
            id=big_fragment,
            revision=1,
            created_at=ANCHOR,
            kind=FragmentKind.TEXT,
            payload_ref=f"payload:{big_fragment}",
            provenance=_plain_provenance_for("principal:agent"),
            commitment_refs=(),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
    )
    _snapshot_prereqs(core)
    from agora.contracts import (
        ConversationEpisode,
        EventKind,
        InboundEvent,
        RecoveryStatus,
    )

    # the event is a member of the episode (bead 93h): selection reads
    # membership, not raw ingestion rows
    core.put_record(
        InboundEvent(
            id="evt:big",
            revision=1,
            created_at=ANCHOR,
            source_event_id="evt:big",
            journal_seq=1,
            kind=EventKind.MESSAGE_CREATED,
            actor_ref=RecordRef(id="principal:agent", revision=1),
            occurred_at=ANCHOR,
            ingested_at=ANCHOR,
            source_audience_ref=_audience_for(core, "room:a"),
            fragment_refs=(RecordRef(id=big_fragment, revision=1),),
            reply_target_refs=(),
            target_refs=(),
            observed_trigger_refs=(),
            recovery=RecoveryStatus.COMPLETE,
        )
    )
    core.put_record(
        ConversationEpisode(
            id="episode:1",
            revision=1,
            created_at=ANCHOR,
            container_id="room:a",
            member_event_refs=(RecordRef(id="evt:big", revision=1),),
            reply_edges=(),
            topic_refs=(),
            summary_ref=None,
            episode_revision=1,
            pending_intent_refs=(),
        )
    )
    snapshot = context.build_snapshot(
        identity_ref=RecordRef(id="identity:agent", revision=1),
        intent_ref=RecordRef(id="intent:1", revision=1),
        audience_ref=_audience_for(core, "room:a"),
        episode_ref=RecordRef(id="episode:1", revision=1),
        sink_ref=RecordRef(id="sink:scripted", revision=1),
        effect_capable=False,
        call_id="snap:budget",
    )
    selected_ids = [ref.id for ref in snapshot.selected_fragment_refs]
    assert big_fragment in selected_ids
    assert "fragment:memory:memory:big" not in selected_ids


def _audience_for(core: CoreStore, destination_id: str) -> RecordRef:
    from agora.contracts import (
        AudienceSnapshot,
        Completeness,
        EnvironmentKind,
        HistoryVisibility,
        Visibility,
    )

    if core.get_record(f"audience:{destination_id}") is None:
        core.put_record(
            AudienceSnapshot(
                id=f"audience:{destination_id}",
                revision=1,
                created_at=ANCHOR,
                destination_id=destination_id,
                environment_kind=EnvironmentKind.ROOM,
                visibility=Visibility.PUBLIC,
                known_principal_refs=(),
                unknown_readers=True,
                history_visibility=HistoryVisibility.PUBLIC_HISTORY,
                completeness=Completeness.OBSERVED,
                disclosure_context_revision=1,
                valid_until=None,
            )
        )
    return RecordRef(id=f"audience:{destination_id}", revision=1)


def _intent_for(core: CoreStore) -> RecordRef:
    from agora.contracts import Intent, IntentState

    seed_principal(core)
    core.put_payload("payload:goal:budget", "goal", owner_ref="identity:agent")
    core.put_record(
        ContentFragment(
            id="fragment:goal:budget",
            revision=1,
            created_at=ANCHOR,
            kind=FragmentKind.TEXT,
            payload_ref="payload:goal:budget",
            provenance=_plain_provenance_for("principal:agent"),
            commitment_refs=(),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
    )
    core.put_record(
        Intent(
            id="intent:1",
            revision=1,
            created_at=ANCHOR,
            creator_ref=RecordRef(id="principal:agent", revision=1),
            goal_fragment_ref=RecordRef(id="fragment:goal:budget", revision=1),
            purpose_class="social-participation",
            observed_trigger_refs=(),
            endorsement_revision=1,
            producer_coverage_ref=None,
            runtime_task_root_ids=("root:b",),
            parent_work_ref=None,
            allowed_destination_refs=("room:a",),
            due_at=None,
            expires_at=None,
            state=IntentState.OPEN,
        )
    )
    return RecordRef(id="intent:1", revision=1)


def _snapshot_prereqs(core: CoreStore) -> None:
    from agora.contracts import AgentIdentity, ComputeSink, SinkKind, SinkStatus

    seed_principal(core)
    core.put_record(
        AgentIdentity(
            id="identity:agent",
            revision=1,
            created_at=ANCHOR,
            principal_ref=RecordRef(id="principal:agent", revision=1),
            preference_refs=(),
            autobiography_refs=(),
            commitment_refs=(),
            active_interest_refs=(),
        )
    )
    core.put_record(
        ComputeSink(
            id="sink:scripted",
            revision=1,
            created_at=ANCHOR,
            kind=SinkKind.SCRIPTED_TEST,
            configured_endpoint=None,
            permitted_data_classes=("public",),
            capability_ref=None,
            status=SinkStatus.ENABLED,
        )
    )
    _intent_for(core)


def test_durable_promotion_requires_candidate(tmp_path: Path) -> None:
    from agora.context_memory import MemoryStateError
    from agora.stores import CoreStore as Store

    core = Store(tmp_path / "core.sqlite")
    context = _context_for(core)
    seed_memory(core, "memory:skip", stage=MemoryStage.SOCIAL)
    with pytest.raises(MemoryStateError, match="CANDIDATE"):
        context.stage_memory(RecordRef(id="memory:skip", revision=1), MemoryStage.DURABLE)
    seed_memory(core, "memory:eph", stage=MemoryStage.EPHEMERAL)
    with pytest.raises(MemoryStateError, match="CANDIDATE"):
        context.stage_memory(RecordRef(id="memory:eph", revision=1), MemoryStage.DURABLE)
    # the staged path still works: EPHEMERAL -> CANDIDATE -> DURABLE
    candidate = context.stage_memory(RecordRef(id="memory:eph", revision=1), MemoryStage.CANDIDATE)
    durable = context.stage_memory(candidate, MemoryStage.DURABLE)
    assert durable.revision == 3


def test_stale_memory_cannot_be_promoted_or_corrected(tmp_path: Path) -> None:
    from agora.context_memory import MemoryStateError
    from agora.stores import CoreStore as Store

    core = Store(tmp_path / "core.sqlite")
    context = _context_for(core)
    ref = seed_memory(core, "memory:victim")
    context.correct_memory(ref, "corrected once")
    current = core.current_ref("memory:victim")
    assert current is not None and current.revision == 2
    # flip the memory STALE (as an invalidation would) and try to resurrect
    from dataclasses import replace as dc_replace

    stale_record = core.get_record("memory:victim", current.revision)
    assert isinstance(stale_record, MemoryRecord)
    core.promote_memory(
        dc_replace(stale_record, revision=3, status=MemoryStatus.STALE),
        expiry_ms=EXPIRY_MS,
    )
    with pytest.raises(MemoryStateError, match="STALE"):
        context.stage_memory(RecordRef(id="memory:victim", revision=3), MemoryStage.CANDIDATE)
    with pytest.raises(MemoryStateError, match="STALE"):
        context.correct_memory(RecordRef(id="memory:victim", revision=3), "resurrected")


def test_invalidation_revisions_stamp_correction_time(tmp_path: Path) -> None:
    from agora.contracts import DependencyKind, Derivation, DerivationState, ProducerKind
    from agora.ports import VirtualClock
    from agora.stores import CoreStore as Store

    core = Store(tmp_path / "core.sqlite")
    clock = VirtualClock(ANCHOR, start_ms=500_000)  # correction time != ANCHOR
    context = ContextMemory(core, clock)
    seed_memory(core, "memory:src")
    core.put_payload("payload:fragment:memory:summ2", "summary", owner_ref="summ2")
    core.put_record(
        ContentFragment(
            id="fragment:memory:summ2",
            revision=1,
            created_at=ANCHOR,
            kind=FragmentKind.TEXT,
            payload_ref="payload:fragment:memory:summ2",
            provenance=_plain_provenance_for("principal:agent"),
            commitment_refs=(),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
    )
    core.put_record(
        ProducerCoverage(
            id="coverage:summ2",
            revision=1,
            created_at=ANCHOR,
            producer_kind=ProducerKind.SUMMARY,
            input_fragment_refs=(RecordRef(id="fragment:memory:memory:src", revision=1),),
            input_view_refs=(),
            input_intent_refs=(),
            input_policy_refs=(),
            sink_ref=None,
            call_id="summ2",
            runtime_epoch=core.runtime_epoch,
        )
    )
    core.put_record(
        Derivation(
            id="derivation:summ2",
            revision=1,
            created_at=ANCHOR,
            output_fragment_ref=RecordRef(id="fragment:memory:summ2", revision=1),
            producer_coverage_ref=RecordRef(id="coverage:summ2", revision=1),
            claimed_parent_refs=(RecordRef(id="fragment:memory:memory:src", revision=1),),
            transform_id=None,
            authorization_refs=(),
            inherited_commitment_refs=(),
            input_version_vector=(RecordRef(id="fragment:memory:memory:src", revision=1),),
            state=DerivationState.CURRENT,
        )
    )
    core.add_dependency(
        RecordRef(id="derivation:summ2", revision=1),
        RecordRef(id="fragment:memory:memory:src", revision=1),
        DependencyKind.PRODUCER_INPUT,
    )
    context.correct_memory(RecordRef(id="memory:src", revision=1), "corrected later")
    flipped = core.get_record("derivation:summ2")
    assert isinstance(flipped, Derivation)
    assert flipped.state == DerivationState.STALE
    assert flipped.created_at != ANCHOR  # the invalidation time is recorded


def _derive(
    core: CoreStore,
    derivation_id: str,
    source_fragment_id: str,
    text: str,
) -> str:
    """A derivation whose output fragment is retained as a memory."""
    from agora.contracts import DependencyKind, Derivation, DerivationState, ProducerKind

    output_fragment_id = f"fragment:memory:{derivation_id}"
    core.put_payload(f"payload:{output_fragment_id}", text, owner_ref=derivation_id)
    core.put_record(
        ContentFragment(
            id=output_fragment_id,
            revision=1,
            created_at=ANCHOR,
            kind=FragmentKind.TEXT,
            payload_ref=f"payload:{output_fragment_id}",
            provenance=_plain_provenance_for("principal:agent"),
            commitment_refs=(),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
    )
    core.put_record(
        ProducerCoverage(
            id=f"coverage:{derivation_id}",
            revision=1,
            created_at=ANCHOR,
            producer_kind=ProducerKind.SUMMARY,
            input_fragment_refs=(RecordRef(id=source_fragment_id, revision=1),),
            input_view_refs=(),
            input_intent_refs=(),
            input_policy_refs=(),
            sink_ref=None,
            call_id=derivation_id,
            runtime_epoch=core.runtime_epoch,
        )
    )
    core.put_record(
        Derivation(
            id=derivation_id,
            revision=1,
            created_at=ANCHOR,
            output_fragment_ref=RecordRef(id=output_fragment_id, revision=1),
            producer_coverage_ref=RecordRef(id=f"coverage:{derivation_id}", revision=1),
            claimed_parent_refs=(RecordRef(id=source_fragment_id, revision=1),),
            transform_id=None,
            authorization_refs=(),
            inherited_commitment_refs=(),
            input_version_vector=(RecordRef(id=source_fragment_id, revision=1),),
            state=DerivationState.CURRENT,
        )
    )
    core.add_dependency(
        RecordRef(id=derivation_id, revision=1),
        RecordRef(id=source_fragment_id, revision=1),
        DependencyKind.PRODUCER_INPUT,
    )
    # retain the derivation's OWN output fragment as a scoped memory
    core.promote_memory(
        MemoryRecord(
            id=f"memory:{derivation_id}",
            revision=1,
            created_at=ANCHOR,
            assertion_fragment_ref=RecordRef(id=output_fragment_id, revision=1),
            stage=MemoryStage.CANDIDATE,
            scope_kind=ScopeKind.ROOM,
            scope_ref="room:a",
            custodian_refs=(),
            subject_refs=(),
            commitment_refs=(),
            assertion_status=AssertionStatus.OBSERVED_STATEMENT,
            supersedes_ref=None,
            valid_until=None,
            status=MemoryStatus.ACTIVE,
        ),
        expiry_ms=EXPIRY_MS,
    )
    return output_fragment_id


def test_correction_invalidates_transitively(tmp_path: Path) -> None:
    """source -> summary A -> summary B -> snapshot propagates all the way
    down (imi feedback on task 3), not one level."""
    from agora.contracts import ContextSnapshot, Derivation, DerivationState
    from agora.ports import VirtualClock
    from agora.stores import CoreStore as Store

    core = Store(tmp_path / "core.sqlite")
    context = ContextMemory(core, VirtualClock(ANCHOR, start_ms=900_000))
    seed_memory(core, "memory:src")
    summary_a_fragment = _derive(core, "derivation:a", "fragment:memory:memory:src", "summary A")
    summary_b_fragment = _derive(core, "derivation:b", summary_a_fragment, "summary B of A")
    _snapshot_prereqs(core)
    from agora.contracts import ConversationEpisode

    core.put_record(
        ConversationEpisode(
            id="episode:chain",
            revision=1,
            created_at=ANCHOR,
            container_id="room:a",
            member_event_refs=(),
            reply_edges=(),
            topic_refs=(),
            summary_ref=None,
            episode_revision=1,
            pending_intent_refs=(),
        )
    )
    snapshot = context.build_snapshot(
        identity_ref=RecordRef(id="identity:agent", revision=1),
        intent_ref=RecordRef(id="intent:1", revision=1),
        audience_ref=_audience_for(core, "room:a"),
        episode_ref=RecordRef(id="episode:chain", revision=1),
        sink_ref=RecordRef(id="sink:scripted", revision=1),
        effect_capable=False,
        call_id="snap:chain",
    )
    assert summary_b_fragment in [ref.id for ref in snapshot.selected_fragment_refs]

    context.correct_memory(RecordRef(id="memory:src", revision=1), "corrected source")
    derivation_a = core.get_record("derivation:a")
    derivation_b = core.get_record("derivation:b")
    assert isinstance(derivation_a, Derivation) and derivation_a.state == DerivationState.STALE
    assert isinstance(derivation_b, Derivation) and derivation_b.state == DerivationState.STALE
    for memory_id in ("memory:derivation:a", "memory:derivation:b"):
        memory = core.get_record(memory_id)
        assert isinstance(memory, MemoryRecord)
        assert memory.status == MemoryStatus.STALE
    invalidated = core.get_record(snapshot.id)
    assert isinstance(invalidated, ContextSnapshot)
    assert invalidated.status.value == "INVALID"


def test_episode_selection_discriminates_foreign_episodes(tmp_path: Path) -> None:
    """Bead 93h two-episode discrimination: fragments ingested under
    another conversation's episode never enter this turn's snapshot — not
    its selected fragments, not its producer-coverage inputs, and not
    propose_memory's acceptable lineage."""
    from agora.context_memory import MemoryStateError
    from agora.contracts import ConversationEpisode, RuntimeOrigin
    from tests.fixtures_review import ReviewStack, feed_event, make_intent

    stack = ReviewStack(tmp_path)
    feed_event(stack, 1)  # episode:room:public -> fragment:evt:1
    feed_event(stack, 2)  # episode:room:public -> fragment:evt:2
    feed_event(stack, 3, episode_id="episode:room:other", container_id="room:other")
    feed_event(stack, 4, episode_id="episode:room:other", container_id="room:other")

    # membership lives on the episode record, in journal order
    episode = stack.core.get_record("episode:room:public")
    assert isinstance(episode, ConversationEpisode)
    assert [ref.id for ref in episode.member_event_refs] == ["evt:1", "evt:2"]

    episode_ref = stack.core.current_ref("episode:room:public")
    assert episode_ref is not None
    snapshot = stack.context_memory.build_snapshot(
        identity_ref=RecordRef(id=stack.identity.id, revision=1),
        intent_ref=make_intent(stack, "intent:episode-scope"),
        audience_ref=RecordRef(id=stack.audience.id, revision=1),
        episode_ref=episode_ref,
        sink_ref=stack.sink_ref,
        effect_capable=True,
        call_id="snapshot:episode-scope",
    )

    selected = [ref.id for ref in snapshot.selected_fragment_refs]
    assert selected == ["fragment:evt:1", "fragment:evt:2"]
    coverage = stack.core.get_record(snapshot.producer_coverage_ref.id)
    assert isinstance(coverage, ProducerCoverage)
    covered = [ref.id for ref in coverage.input_fragment_refs]
    assert covered == ["fragment:evt:1", "fragment:evt:2"]

    # a foreign fragment cited as memory lineage is a typed hold: it was
    # never supplied by this turn's runtime context
    origin = RuntimeOrigin(
        actor_ref=RecordRef(id="principal:agent", revision=1),
        work_ref=RecordRef(id="work:episode-scope", revision=1),
        snapshot_ref=RecordRef(id=snapshot.id, revision=snapshot.revision),
        trigger_event_refs=(),
        producer_coverage_ref=snapshot.producer_coverage_ref,
        runtime_epoch=stack.core.runtime_epoch,
    )
    with pytest.raises(MemoryStateError) as excinfo:
        stack.context_memory.propose_memory(
            assertion_text="the other room said something",
            source_refs=(RecordRef(id="fragment:evt:3", revision=1),),
            requested_stage=MemoryStage.SOCIAL,
            requested_scope_kind=ScopeKind.ROOM,
            scope_ref="room:public",
            existing_memory_ref=None,
            runtime_origin=origin,
        )
    assert excinfo.value.code.value == "INVALID_REFERENCE"


def test_event_binding_repairs_on_replayed_duplicate(tmp_path: Path) -> None:
    """Review findings on bead 93h: an entry ingested before its fragment
    or episode records exist is not bound (fail-closed — the ingestion row
    still lands and the cursor still advances), and a crash-replayed
    duplicate heals the binding, so an event is never permanently outside
    its episode's context."""
    from agora.contracts import ConversationEpisode, InboundEvent
    from agora.stores import JournalEntry
    from tests.fixtures_review import (
        ReviewStack,
        _event_planner,
        ensure_audience,
        make_intent,
    )

    stack = ReviewStack(tmp_path)
    # mis-ordered fixture: the journal row lands before its fragment exists
    stack.transport.append_events(
        (
            JournalEntry(
                journal_seq=1,
                source_event_id="evt:1",
                source_revision=1,
                kind="MESSAGE_CREATED",
                payload_ref="payload:evt:1",
                fragment_refs=("fragment:evt:1",),
                recovery="COMPLETE",
            ),
        )
    )
    episode_id = "episode:repair"
    planner = _event_planner(
        episode_id,
        actor_ref=RecordRef(id="principal:agent", revision=1),
        source_audience_ref=ensure_audience(stack, "room:public"),
    )
    receipt = stack.coordinator.ingest(stack.transport, consumer_id="core-1", event_planner=planner)
    assert receipt.applied == (1,)  # the row landed; enrichment skipped
    assert stack.core.get_record("evt:1") is None

    # fixture order corrected after the fact: fragment + episode now exist
    stack.core.put_payload("payload:evt:1", "hello hermes repaired", owner_ref="evt:1")
    stack.core.put_record(
        ContentFragment(
            id="fragment:evt:1",
            revision=1,
            created_at=stack.clock.now_rfc3339(),
            kind=FragmentKind.TEXT,
            payload_ref="payload:evt:1",
            provenance=_plain_provenance_for("principal:agent"),
            commitment_refs=(),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
    )
    stack.core.put_record(
        ConversationEpisode(
            id=episode_id,
            revision=1,
            created_at=stack.clock.now_rfc3339(),
            container_id="room:public",
            member_event_refs=(),
            reply_edges=(),
            topic_refs=(),
            summary_ref=None,
            episode_revision=1,
            pending_intent_refs=(),
        )
    )

    # crash-replay: the entry returns as a duplicate and the binding heals
    stack.core._conn.execute("UPDATE core_meta SET value = 0 WHERE key = 'cursor'")
    replay = stack.coordinator.ingest(stack.transport, consumer_id="core-1", event_planner=planner)
    assert replay.duplicates == (1,)
    assert isinstance(stack.core.get_record("evt:1"), InboundEvent)
    episode = stack.core.get_record(episode_id)
    assert isinstance(episode, ConversationEpisode)
    assert [ref.id for ref in episode.member_event_refs] == ["evt:1"]

    episode_ref = stack.core.current_ref(episode_id)
    assert episode_ref is not None
    snapshot = stack.context_memory.build_snapshot(
        identity_ref=RecordRef(id=stack.identity.id, revision=1),
        intent_ref=make_intent(stack, "intent:repair"),
        audience_ref=RecordRef(id=stack.audience.id, revision=1),
        episode_ref=episode_ref,
        sink_ref=stack.sink_ref,
        effect_capable=True,
        call_id="snapshot:repair",
    )
    assert "fragment:evt:1" in [ref.id for ref in snapshot.selected_fragment_refs]


def test_gap_rows_never_become_episode_members(tmp_path: Path) -> None:
    """Review finding on bead 93h: GAP-recovery rows are transport
    bookkeeping — even a planner that binds everything cannot make a gap
    marker an episode member or an InboundEvent."""
    from agora.contracts import ConversationEpisode
    from agora.stores import JournalEntry
    from tests.fixtures_review import ReviewStack, _event_planner, ensure_audience

    stack = ReviewStack(tmp_path)
    stack.core.put_record(
        ConversationEpisode(
            id="episode:gaps",
            revision=1,
            created_at=stack.clock.now_rfc3339(),
            container_id="room:public",
            member_event_refs=(),
            reply_edges=(),
            topic_refs=(),
            summary_ref=None,
            episode_revision=1,
            pending_intent_refs=(),
        )
    )
    stack.transport.append_events(
        (
            JournalEntry(
                journal_seq=1,
                source_event_id="gap:1",
                source_revision=1,
                kind="CONNECTION_GAP",
                payload_ref="",
                fragment_refs=(),
                recovery="GAP",
            ),
        )
    )
    receipt = stack.coordinator.ingest(
        stack.transport,
        consumer_id="core-1",
        event_planner=_event_planner(
            "episode:gaps",
            actor_ref=RecordRef(id="principal:agent", revision=1),
            source_audience_ref=ensure_audience(stack, "room:public"),
        ),
    )
    assert receipt.applied == (1,)  # bookkeeping still lands and advances
    assert stack.core.get_record("gap:1") is None
    episode = stack.core.get_record("episode:gaps")
    assert isinstance(episode, ConversationEpisode)
    assert episode.member_event_refs == ()


def test_membership_follows_event_revisions_in_place(tmp_path: Path) -> None:
    """Bead sq5: a same source_event_id at a new source_revision
    (MESSAGE_UPDATED / MESSAGE_DELETED) is the same member followed at its
    current revision — conversational position kept, episode record
    revision bumped — so new turns resolve edited content, snapshots that
    pinned the episode earlier keep their as-of-turn view, and a deleted
    event is a tombstone contributing nothing."""
    from agora.contracts import ConversationEpisode
    from agora.stores import JournalEntry
    from tests.fixtures_review import ReviewStack, _event_planner, ensure_audience, make_intent

    stack = ReviewStack(tmp_path)
    episode_id = "episode:edits"
    stack.core.put_record(
        ConversationEpisode(
            id=episode_id,
            revision=1,
            created_at=stack.clock.now_rfc3339(),
            container_id="room:public",
            member_event_refs=(),
            reply_edges=(),
            topic_refs=(),
            summary_ref=None,
            episode_revision=1,
            pending_intent_refs=(),
        )
    )
    planner = _event_planner(
        episode_id,
        actor_ref=RecordRef(id="principal:agent", revision=1),
        source_audience_ref=ensure_audience(stack, "room:public"),
    )

    def feed(seq: int, event_id: str, revision: int, kind: str, fragment_id: str) -> None:
        if stack.core.current_ref(fragment_id) is None:
            stack.core.put_payload(
                f"payload:{fragment_id}", f"content {fragment_id}", owner_ref=event_id
            )
            stack.core.put_record(
                ContentFragment(
                    id=fragment_id,
                    revision=1,
                    created_at=stack.clock.now_rfc3339(),
                    kind=FragmentKind.TEXT,
                    payload_ref=f"payload:{fragment_id}",
                    provenance=_plain_provenance_for("principal:agent"),
                    commitment_refs=(),
                    eligibility_status=EligibilityStatus.ACTIVE,
                )
            )
        stack.transport.append_events(
            (
                JournalEntry(
                    journal_seq=seq,
                    source_event_id=event_id,
                    source_revision=revision,
                    kind=kind,
                    payload_ref=f"payload:{fragment_id}",
                    fragment_refs=(fragment_id,) if fragment_id else (),
                    recovery="COMPLETE",
                ),
            )
        )
        stack.coordinator.ingest(stack.transport, consumer_id="core-1", event_planner=planner)

    feed(1, "evt:1", 1, "MESSAGE_CREATED", "fragment:evt:1:v1")
    feed(2, "evt:2", 1, "MESSAGE_CREATED", "fragment:evt:2")
    pre_edit = stack.core.current_ref(episode_id)
    assert pre_edit is not None

    # the edit: same event id, upstream revision 2, new content
    feed(3, "evt:1", 2, "MESSAGE_UPDATED", "fragment:evt:1:v2")

    episode = stack.core.get_record(episode_id)
    assert isinstance(episode, ConversationEpisode)
    assert [(ref.id, ref.revision) for ref in episode.member_event_refs] == [
        ("evt:1", 2),
        ("evt:2", 1),
    ]  # followed in place: position kept, revision current

    def snapshot_for(ref: RecordRef, call_id: str):
        return stack.context_memory.build_snapshot(
            identity_ref=RecordRef(id=stack.identity.id, revision=1),
            intent_ref=make_intent(stack, f"intent:{call_id}"),
            audience_ref=RecordRef(id=stack.audience.id, revision=1),
            episode_ref=ref,
            sink_ref=stack.sink_ref,
            effect_capable=True,
            call_id=f"snapshot:{call_id}",
        )

    current_episode = stack.core.current_ref(episode_id)
    assert current_episode is not None
    current = snapshot_for(current_episode, "edit-current")
    assert [ref.id for ref in current.selected_fragment_refs] == [
        "fragment:evt:1:v2",
        "fragment:evt:2",
    ]
    pinned = snapshot_for(pre_edit, "edit-pinned")
    assert [ref.id for ref in pinned.selected_fragment_refs] == [
        "fragment:evt:1:v1",
        "fragment:evt:2",
    ]  # as-of-turn: the pre-edit episode revision still shows v1

    # the delete: same event id, revision 3, no fragments — a tombstone
    stack.transport.append_events(
        (
            JournalEntry(
                journal_seq=4,
                source_event_id="evt:2",
                source_revision=2,
                kind="MESSAGE_DELETED",
                payload_ref="",
                fragment_refs=(),
                recovery="COMPLETE",
            ),
        )
    )
    stack.coordinator.ingest(stack.transport, consumer_id="core-1", event_planner=planner)
    post_delete_episode = stack.core.current_ref(episode_id)
    assert post_delete_episode is not None
    after_delete = snapshot_for(post_delete_episode, "delete-current")
    assert [ref.id for ref in after_delete.selected_fragment_refs] == ["fragment:evt:1:v2"]


def test_view_revocation_invalidates_admitting_snapshots(tmp_path: Path) -> None:
    """Bead l34 / I14: revoking a view invalidates every active snapshot
    that admitted it (revocation is not limited to retrieval-time
    eligibility), idempotently, in one transaction."""
    from agora.contracts import ContextSnapshot, DependencyKind, MemoryView, RecordStatus
    from tests.fixtures_review import (
        ReviewStack,
        comparison_view,
        feed_event,
        make_intent,
        review_snapshot,
        seed_five_classes,
    )

    stack = ReviewStack(tmp_path)
    seed_five_classes(stack)  # use:finance-fields backs the view
    intent = make_intent(stack, "intent:revoke")
    feed_event(stack)  # fragment:evt:1 behind the view
    view = comparison_view(
        stack,
        "view:revoked",
        call_id="call:revoke",
        fragment_ref=RecordRef(id="fragment:evt:1", revision=1),
        authorization_id="use:finance-fields",
    )
    snapshot = review_snapshot(stack, "call:revoke", intent, views=(view,))
    # the dependency edge production build_snapshot writes for admitted
    # views — the revocation walk follows exactly this edge
    stack.core.add_dependency(
        RecordRef(id=snapshot.id, revision=snapshot.revision),
        RecordRef(id=view.id, revision=view.revision),
        DependencyKind.PRODUCER_INPUT,
    )
    revoked_ref = stack.context_memory.revoke_view(RecordRef(id=view.id, revision=1))
    view_record = stack.core.get_record(revoked_ref.id, revoked_ref.revision)
    assert isinstance(view_record, MemoryView) and view_record.status == RecordStatus.REVOKED
    invalidated = stack.core.get_record(snapshot.id)
    assert isinstance(invalidated, ContextSnapshot)
    assert invalidated.status.value == "INVALID"
    # idempotent: revoking again returns the standing revocation
    again = stack.context_memory.revoke_view(revoked_ref)
    assert again == revoked_ref


def test_revocation_crash_rolls_back_and_retries(tmp_path: Path) -> None:
    """The revocation's crash-rollback claim is pinned: a crash inside the
    invalidation-job boundary leaves the view active and the snapshots
    untouched, and the revocation re-runs cleanly."""
    from agora.contracts import ContextSnapshot, DependencyKind, MemoryView, RecordStatus
    from agora.ports import CrashCheckpoint, InjectedCrash
    from tests.fixtures_review import (
        ReviewStack,
        comparison_view,
        feed_event,
        make_intent,
        review_snapshot,
        seed_five_classes,
    )

    stack = ReviewStack(tmp_path)
    seed_five_classes(stack)
    feed_event(stack)
    intent = make_intent(stack, "intent:revoke-crash")
    view = comparison_view(
        stack,
        "view:crash",
        call_id="call:revoke-crash",
        fragment_ref=RecordRef(id="fragment:evt:1", revision=1),
        authorization_id="use:finance-fields",
    )
    snapshot = review_snapshot(stack, "call:revoke-crash", intent, views=(view,))
    stack.core.add_dependency(
        RecordRef(id=snapshot.id, revision=snapshot.revision),
        RecordRef(id=view.id, revision=view.revision),
        DependencyKind.PRODUCER_INPUT,
    )

    stack.injector.arm(CrashCheckpoint.DURING_CORRECTION_JOB)
    with pytest.raises(InjectedCrash):
        stack.context_memory.revoke_view(RecordRef(id=view.id, revision=1))
    stack.injector.disarm(CrashCheckpoint.DURING_CORRECTION_JOB)
    untouched_view = stack.core.get_record(view.id)
    assert isinstance(untouched_view, MemoryView) and untouched_view.status == RecordStatus.ACTIVE
    untouched_snapshot = stack.core.get_record(snapshot.id)
    assert (
        isinstance(untouched_snapshot, ContextSnapshot)
        and untouched_snapshot.status.value == "ACTIVE"
    )

    revoked = stack.context_memory.revoke_view(RecordRef(id=view.id, revision=1))
    assert revoked.revision == 2
    invalidated = stack.core.get_record(snapshot.id)
    assert isinstance(invalidated, ContextSnapshot)
    assert invalidated.status.value == "INVALID"
