"""M2 task 2.2: ContextMemory.request_access over the seeded registries.

Spec scenarios: granted partial view (FIELDS precision under a covering
authorization) and denied-carries-nothing (authority chain resolved, use
not authorized). Runs on the real stores with the five-class fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

from agora.context_memory import ContextMemory
from agora.contracts import (
    AccessStatus,
    Commitment,
    IntendedUse,
    MemoryAccessRequest,
    MemoryAccessResult,
    MemoryView,
    ProducerCoverage,
    ReasonCode,
    RecordRef,
    RequestedPrecision,
    ViewKind,
)
from agora.runner import ReplayRunner, RunManifest
from agora.stores import CoreStore
from tests.fixtures_m2 import (
    FINANCE_RESOURCE,
    THIRD_PARTY_RESOURCE,
    five_class_registries,
    manifest_with_registries,
)


def boot(tmp_path: Path) -> tuple[CoreStore, ContextMemory, ReplayRunner, RecordRef]:
    manifest = RunManifest.from_payload(manifest_with_registries(five_class_registries()))
    runner = ReplayRunner(manifest, tmp_path / "run")
    runner.run()  # one quiet turn: identity, intents, work items exist
    context = ContextMemory(runner.core, runner.clock)
    row = runner.core._conn.execute(
        "SELECT record_id, revision FROM core_records"
        " WHERE record_type = 'Intent' AND is_current = 1 ORDER BY record_id LIMIT 1"
    ).fetchone()
    assert row is not None
    return runner.core, context, runner, RecordRef(id=row["record_id"], revision=row["revision"])


def make_request(
    *,
    request_id: str,
    intent_ref: RecordRef,
    resource_id: str | None = THIRD_PARTY_RESOURCE,
    data_class: str | None = None,
    intended_use: IntendedUse = IntendedUse.INTERNAL_ONLY,
    precision: RequestedPrecision = RequestedPrecision.FIELDS,
    necessity: str = "needed to answer the budget question safely",
) -> MemoryAccessRequest:
    return MemoryAccessRequest(
        id=f"request:{request_id}",
        revision=1,
        created_at="2026-10-12T12:00:00Z",
        request_id=request_id,
        intent_ref=intent_ref,
        resource_id=resource_id,
        data_class=data_class,
        requested_precision=precision,
        requested_fields=(),
        transform_id=None,
        audience_ref=RecordRef(id="audience:room:public", revision=1),
        intended_use=intended_use,
        necessity_text=necessity,
        necessity_payload_ref=None,
        trigger_refs=(),
        runtime_origin=None,
    )


def test_granted_partial_view_persists_chain(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    result = context.request_access(
        make_request(
            request_id="probe:finance", intent_ref=intent_ref, resource_id=FINANCE_RESOURCE
        )
    )
    assert result.access_status == AccessStatus.GRANTED
    assert result.view_kind == ViewKind.PARTIAL
    assert result.permitted_uses == (IntendedUse.INTERNAL_ONLY,)
    assert [ref.id for ref in result.authorization_refs] == ["use:finance-fields"]

    view = (
        core.get_record(result.view_ref.id, result.view_ref.revision) if result.view_ref else None
    )
    assert isinstance(view, MemoryView)
    assert view.precision == RequestedPrecision.FIELDS
    assert [ref.id for ref in view.returned_fragment_refs] == [FINANCE_RESOURCE]
    assert [ref.id for ref in view.authorization_refs] == ["use:finance-fields"]
    assert [ref.id for ref in view.inherited_commitment_refs] == ["commit:finance-strong"]
    assert view.audience_bounds == ("room:public",)
    assert view.status.value == "ACTIVE"
    coverage = core.get_record(view.producer_coverage_ref.id)
    assert isinstance(coverage, ProducerCoverage)

    # necessity text is out-of-line: the stored request carries only the ref
    stored_request = core.get_record("request:probe:finance")
    assert stored_request is not None
    payload_json = core._conn.execute(
        "SELECT payload_json FROM core_records WHERE record_id = 'request:probe:finance'"
    ).fetchone()["payload_json"]
    stored_payload = json.loads(payload_json)
    assert stored_payload["necessity_text"] is None  # swapped out-of-line
    assert stored_payload["necessity_payload_ref"].startswith("payload:")
    assert "budget question" not in payload_json
    assert "budget question" in (core.get_payload(stored_payload["necessity_payload_ref"]) or "")

    # the inherited commitment is the real seeded STRONG commitment
    commitment = core.get_record("commit:finance-strong")
    assert isinstance(commitment, Commitment)
    assert commitment.expression_allowed is False

    # GRANTED access is durable via its records; the audit row is absent
    rows = core._conn.execute(
        "SELECT count(*) FROM core_audit WHERE entity_ref = ?", (result.id,)
    ).fetchone()[0]
    assert rows == 0


def test_denied_carries_nothing_and_audits(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    result = context.request_access(
        make_request(
            request_id="probe:third-party", intent_ref=intent_ref, resource_id=THIRD_PARTY_RESOURCE
        )
    )
    assert result.access_status == AccessStatus.DENIED
    assert result.view_kind == ViewKind.NONE
    assert result.view_ref is None
    assert result.permitted_uses == ()
    assert result.authorization_refs == ()
    assert result.inherited_commitment_refs == ()
    assert result.transform_certificate_ref is None
    assert ReasonCode.POLICY_FORBIDDEN in result.reason_codes
    # no view record exists for the denied request
    assert core.get_record("view:probe:third-party") is None
    # registry-only audit row for the denial
    row = core._conn.execute(
        "SELECT reason_code FROM core_audit WHERE entity_ref = ?", (result.id,)
    ).fetchone()
    assert row is not None and row["reason_code"] == "POLICY_FORBIDDEN"


def test_pending_authority_when_domain_binding_missing(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    # finance has RESOURCE_USE bindings only; observable influence needs
    # DISCLOSURE, which no fixture grants for this resource
    result = context.request_access(
        make_request(
            request_id="probe:observe",
            intent_ref=intent_ref,
            resource_id=FINANCE_RESOURCE,
            intended_use=IntendedUse.OBSERVABLE_INFLUENCE,
        )
    )
    assert result.access_status == AccessStatus.PENDING_AUTHORITY
    assert ReasonCode.AUTHORITY_MISSING in result.reason_codes
    assert core.get_record("view:probe:observe") is None


def test_data_class_request_grants_across_members(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    result = context.request_access(
        make_request(
            request_id="probe:class",
            intent_ref=intent_ref,
            resource_id=None,
            data_class="financial",
        )
    )
    assert result.access_status == AccessStatus.GRANTED
    assert result.view_kind == ViewKind.PARTIAL
    assert result.view_ref is not None
    view = core.get_record(result.view_ref.id)
    assert isinstance(view, MemoryView)
    assert view.resource_id == "financial"
    assert [ref.id for ref in view.returned_fragment_refs] == [FINANCE_RESOURCE]


def test_request_access_is_idempotent_under_replay(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    request = make_request(
        request_id="probe:finance", intent_ref=intent_ref, resource_id=FINANCE_RESOURCE
    )
    first = context.request_access(request)
    # crash replay: the same virtual moment regenerates byte-identical
    # records; the second call must not conflict
    second = context.request_access(request)
    assert second == first
    assert core.record_revisions("result:probe:finance") == (1,)
    again = core.get_record("result:probe:finance")
    assert isinstance(again, MemoryAccessResult)
    assert again.access_status == AccessStatus.GRANTED
