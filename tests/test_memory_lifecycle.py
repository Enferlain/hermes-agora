"""M2 task 3.1: scoped memory eligibility via core.memory_scope.

The scope index is written transactionally with record promotion;
selection filters by stage/scope/status/expiry on the index before any
payload read; there is no global retrieval index.
"""

from __future__ import annotations

from pathlib import Path

from agora.contracts import (
    AssertionStatus,
    ContentFragment,
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
