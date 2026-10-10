"""Core store tests: versioned records, schema refusal, epochs, payloads,
dependencies, audit safety, and work state (handoff §7)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import agora.stores as stores
from agora.contracts import (
    DependencyKind,
    Principal,
    PrincipalKind,
    RecordRef,
    ValidationError,
    WorkState,
)

UTC = "2026-10-10T00:00:00Z"


def make_principal(revision: int) -> Principal:
    ref = RecordRef(id="principal:agent", revision=1)
    return Principal(
        id="principal:agent",
        revision=revision,
        created_at=UTC,
        kind=PrincipalKind.AGENT,
        verified_bindings=(ref,),
        claimed_aliases=("hermes",),
    )


@pytest.fixture
def core(tmp_path: Path) -> stores.CoreStore:
    return stores.CoreStore(tmp_path / "core.sqlite")


def test_fresh_store_initializes_epoch_and_cursor(core: stores.CoreStore) -> None:
    assert core.runtime_epoch == 0
    assert core.cursor == 0


def test_reopen_bumps_epoch_and_generation(tmp_path: Path) -> None:
    core = stores.CoreStore(tmp_path / "core.sqlite")
    transport = stores.TransportStore(tmp_path / "transport.sqlite")
    assert (core.runtime_epoch, transport.transport_generation) == (0, 0)
    core.close()
    transport.close()
    core2 = stores.CoreStore(tmp_path / "core.sqlite")
    transport2 = stores.TransportStore(tmp_path / "transport.sqlite")
    assert core2.runtime_epoch == 1
    assert transport2.transport_generation == 1


def test_unknown_schema_version_refuses_startup(tmp_path: Path) -> None:
    path = tmp_path / "core.sqlite"
    core = stores.CoreStore(path)
    core.close()
    connection = sqlite3.connect(path)
    connection.execute("UPDATE core_meta SET value = '99' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()
    with pytest.raises(stores.SchemaVersionError):
        stores.CoreStore(path)


def test_schema_digest_mismatch_refuses_startup(tmp_path: Path) -> None:
    path = tmp_path / "core.sqlite"
    core = stores.CoreStore(path)
    core.close()
    connection = sqlite3.connect(path)
    connection.execute("UPDATE core_meta SET value = 'deadbeef' WHERE key = 'schema_digest'")
    connection.commit()
    connection.close()
    with pytest.raises(stores.SchemaVersionError):
        stores.CoreStore(path)


def test_records_append_only_with_current_flip(core: stores.CoreStore) -> None:
    core.put_record(make_principal(1))
    core.put_record(make_principal(2))
    assert core.record_revisions("principal:agent") == (1, 2)
    assert core.current_ref("principal:agent") == RecordRef(id="principal:agent", revision=2)
    assert core.get_record("principal:agent", revision=1) == make_principal(1)
    assert core.get_record("principal:agent") == make_principal(2)


def test_duplicate_revision_is_conflict_not_edit(core: stores.CoreStore) -> None:
    core.put_record(make_principal(1))
    with pytest.raises(stores.RecordConflictError):
        core.put_record(make_principal(1))
    assert core.record_revisions("principal:agent") == (1,)


def test_payload_store_purges_bytes_retaining_metadata(core: stores.CoreStore) -> None:
    core.put_payload("payload:1", "secret-ish synthetic text", owner_ref="test", expires_at_ms=5000)
    assert core.get_payload("payload:1") == "secret-ish synthetic text"
    purged = core.purge_expired_payloads(now_ms=6000)
    assert purged == ("payload:1",)
    assert core.get_payload("payload:1") is None


def test_dependency_graph_rejects_cycles(core: stores.CoreStore) -> None:
    a = RecordRef(id="rec:a", revision=1)
    b = RecordRef(id="rec:b", revision=1)
    c = RecordRef(id="rec:c", revision=1)
    core.add_dependency(b, a, DependencyKind.PRODUCER_INPUT)
    core.add_dependency(c, b, DependencyKind.PRODUCER_INPUT)
    with pytest.raises(stores.DependencyCycleError):
        core.add_dependency(a, c, DependencyKind.PRODUCER_INPUT)
    with pytest.raises(stores.DependencyCycleError):
        core.add_dependency(a, a, DependencyKind.DECLARED)
    assert core.parents_of("rec:c") == ("rec:b",)


def test_audit_rejects_unsafe_content(core: stores.CoreStore) -> None:
    core.audit(operation="access", entity_ref="request:1", reason_code="VIEW_FORBIDDEN", at_ms=1)
    with pytest.raises(ValidationError):
        core.audit(
            operation="access\ncustom reasoning",
            entity_ref="request:1",
            reason_code="VIEW_FORBIDDEN",
            at_ms=1,
        )


def test_work_creation_is_idempotent_on_trigger_key(core: stores.CoreStore) -> None:
    assert core.create_work(
        work_id="work:1",
        trigger_key="turn:evt:1",
        intent_ref="intent:1",
        episode_ref=None,
        runtime_epoch=core.runtime_epoch,
    )
    assert not core.create_work(
        work_id="work:other",
        trigger_key="turn:evt:1",
        intent_ref="intent:1",
        episode_ref=None,
        runtime_epoch=core.runtime_epoch,
    )
    row = core.work_by_trigger_key("turn:evt:1")
    assert row is not None and row["work_id"] == "work:1"


def test_work_transitions_enforce_state_machine(core: stores.CoreStore) -> None:
    core.create_work(
        work_id="work:1",
        trigger_key="turn:evt:1",
        intent_ref="intent:1",
        episode_ref=None,
        runtime_epoch=0,
    )
    core.transition_work("work:1", stores.WorkState.RUNNING, expected_state=stores.WorkState.READY)
    core.transition_work("work:1", stores.WorkState.COMPLETED)
    with pytest.raises(stores.TransitionError):
        core.transition_work("work:1", stores.WorkState.RUNNING)


def test_restart_returns_running_work_to_ready(tmp_path: Path) -> None:
    core = stores.CoreStore(tmp_path / "core.sqlite")
    core.create_work(
        work_id="work:1",
        trigger_key="turn:evt:1",
        intent_ref="intent:1",
        episode_ref=None,
        runtime_epoch=0,
    )
    core.transition_work("work:1", stores.WorkState.RUNNING)
    core._conn.execute(
        "UPDATE core_work SET lease_owner = 'worker:1', lease_until_ms = 999,"
        " main_steps_used = 3 WHERE work_id = 'work:1'"
    )
    core._conn.commit()
    core.close()
    core2 = stores.CoreStore(tmp_path / "core.sqlite")
    row = core2.get_work("work:1")
    assert row is not None
    assert row["state"] == "READY"
    assert row["lease_owner"] is None and row["lease_until_ms"] is None
    # budgets and dedup key retained (§6.1)
    assert row["main_steps_used"] == 3
    assert core2.work_by_trigger_key("turn:evt:1") is not None


def test_text_fields_swap_into_out_of_line_payloads(tmp_path: Path) -> None:
    from agora.contracts import (
        IntendedUse,
        MemoryAccessRequest,
        RecordRef,
        RequestedPrecision,
    )

    core = stores.CoreStore(tmp_path / "core.sqlite")
    request = MemoryAccessRequest(
        id="request:1",
        revision=1,
        created_at=UTC,
        request_id="request:1",
        intent_ref=RecordRef(id="intent:1", revision=1),
        resource_id="resource:finance",
        data_class=None,
        requested_precision=RequestedPrecision.FIELDS,
        requested_fields=(),
        transform_id=None,
        audience_ref=RecordRef(id="audience:dm", revision=1),
        intended_use=IntendedUse.INTERNAL_ONLY,
        necessity_text="private reasoning for the budget question",
        necessity_payload_ref=None,
        trigger_refs=(),
        runtime_origin=None,
    )
    core.put_record(request, text_field_refs={"necessity_text": "necessity_payload_ref"})

    stored = core._conn.execute(
        "SELECT payload_json FROM core_records WHERE record_id = 'request:1'"
    ).fetchone()["payload_json"]
    assert "private reasoning" not in stored
    assert '"necessity_text":null' in stored

    decoded = core.get_record("request:1")
    assert isinstance(decoded, MemoryAccessRequest)
    assert decoded.necessity_text is None
    assert decoded.necessity_payload_ref is not None
    assert (
        core.get_payload(decoded.necessity_payload_ref)
        == "private reasoning for the budget question"
    )


def test_stale_epoch_record_rejected_after_restart(tmp_path: Path) -> None:
    from agora.contracts import WorkItem

    core = stores.CoreStore(tmp_path / "core.sqlite")
    work = WorkItem(
        id="work:1",
        revision=1,
        created_at=UTC,
        intent_ref=RecordRef(id="intent:1", revision=1),
        trigger_key="turn:evt:1",
        episode_ref=None,
        state=WorkState.READY,
        lease_owner=None,
        lease_until_ms=None,
        runtime_epoch=core.runtime_epoch,
        main_steps_used=0,
        submissions_used=0,
        terminal_outcome_ref=None,
    )
    core.put_record(work)
    core.close()

    core2 = stores.CoreStore(tmp_path / "core.sqlite")  # epoch bumped to 1
    with pytest.raises(stores.TransitionError, match="STALE_EPOCH"):
        core2.put_record(
            WorkItem(
                id="work:2",
                revision=1,
                created_at=UTC,
                intent_ref=RecordRef(id="intent:1", revision=1),
                trigger_key="turn:evt:2",
                episode_ref=None,
                state=WorkState.READY,
                lease_owner=None,
                lease_until_ms=None,
                runtime_epoch=0,  # stale
                main_steps_used=0,
                submissions_used=0,
                terminal_outcome_ref=None,
            )
        )


def test_nested_transactions_rejected(core: stores.CoreStore) -> None:
    with pytest.raises(stores.StoreError), core.transaction(), core.transaction():
        pass


def test_outbox_supersession_requires_acknowledgement(tmp_path: Path) -> None:
    transport = stores.TransportStore(tmp_path / "transport.sqlite")
    transport.enqueue_effect(
        logical_effect_id="effect:1",
        proposal_revision=1,
        proposal_digest="0" * 64,
        payload_ref="payload:effect:1",
        auth_ref="auth:1",
        generation=transport.transport_generation,
    )
    with pytest.raises(stores.RecordConflictError):
        transport.enqueue_effect(
            logical_effect_id="effect:1",
            proposal_revision=2,
            proposal_digest="1" * 64,
            payload_ref="payload:effect:1",
            auth_ref="auth:1",
            generation=transport.transport_generation,
        )
    assert transport.acknowledge_supersession("effect:1") is True
    transport.enqueue_effect(
        logical_effect_id="effect:1",
        proposal_revision=2,
        proposal_digest="1" * 64,
        payload_ref="payload:effect:1",
        auth_ref="auth:1",
        generation=transport.transport_generation,
    )
    assert transport.outbox_state("effect:1") == "QUEUED"
    # Once dispatch has started, supersession is too late.
    transport.transition_outbox("effect:1", "DISPATCH_STARTED")
    assert transport.acknowledge_supersession("effect:1") is False


def test_outbox_transitions_are_typed(tmp_path: Path) -> None:
    transport = stores.TransportStore(tmp_path / "transport.sqlite")
    transport.enqueue_effect(
        logical_effect_id="effect:1",
        proposal_revision=1,
        proposal_digest="0" * 64,
        payload_ref="payload:effect:1",
        auth_ref="auth:1",
        generation=0,
    )
    transport.transition_outbox("effect:1", "DISPATCH_STARTED")
    transport.transition_outbox("effect:1", "UNKNOWN")
    with pytest.raises(stores.TransitionError):
        transport.transition_outbox("effect:1", "QUEUED")  # UNKNOWN resolves only via evidence


def test_failed_confirmed_single_retry_path(tmp_path: Path) -> None:
    transport = stores.TransportStore(tmp_path / "transport.sqlite")
    transport.enqueue_effect(
        logical_effect_id="effect:1",
        proposal_revision=1,
        proposal_digest="0" * 64,
        payload_ref="payload:effect:1",
        auth_ref="auth:1",
        generation=0,
    )
    transport.transition_outbox("effect:1", "DISPATCH_STARTED")
    transport.transition_outbox("effect:1", "FAILED_CONFIRMED")
    transport.transition_outbox("effect:1", "QUEUED")  # §6.2 single retry
    with pytest.raises(stores.TransitionError):
        transport.transition_outbox("effect:1", "FAILED_CONFIRMED")


def test_attempts_are_immutable_rows(tmp_path: Path) -> None:
    transport = stores.TransportStore(tmp_path / "transport.sqlite")
    transport.append_attempt(
        attempt_id="attempt:1", effect_id="effect:1", state="DISPATCH_STARTED", started_at_ms=10
    )
    row = transport.get_attempt("attempt:1")
    assert row is not None and row["state"] == "DISPATCH_STARTED"
