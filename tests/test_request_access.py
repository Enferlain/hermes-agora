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
    ContentFragment,
    IntendedUse,
    MemoryAccessRequest,
    MemoryAccessResult,
    MemoryView,
    Pair,
    ProducerCoverage,
    ReasonCode,
    RecordRef,
    RequestedPrecision,
    TransformCertificate,
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
    transform_id: str | None = None,
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
        transform_id=transform_id,
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


# ---------------------------------------------------------------------------
# Task 2.3: the fixed transforms with certificates
# ---------------------------------------------------------------------------


def test_affordability_transform_exposes_predicate_only(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    result = context.request_access(
        make_request(
            request_id="probe:afford",
            intent_ref=intent_ref,
            resource_id=FINANCE_RESOURCE,
            transform_id="FIXED_AFFORDABILITY_PREDICATE",
        )
    )
    assert result.access_status == AccessStatus.GRANTED
    assert result.view_kind == ViewKind.ABSTRACTED
    assert result.transform_certificate_ref is not None
    assert result.view_ref is not None

    view = core.get_record(result.view_ref.id)
    assert isinstance(view, MemoryView)
    assert view.id == "view:probe:afford:transform"
    assert view.precision == RequestedPrecision.ABSTRACT
    assert [ref.id for ref in view.returned_fragment_refs] == ["fragment:transform:probe:afford"]
    assert view.permitted_uses == (IntendedUse.INTERNAL_ONLY,)
    assert [ref.id for ref in view.inherited_commitment_refs] == []  # discharged

    # the output payload exposes the predicate result only — never the amount
    fragment = core.get_record("fragment:transform:probe:afford")
    assert isinstance(fragment, ContentFragment)
    output = json.loads(core.get_payload(fragment.payload_ref) or "{}")
    assert output == {"affordable": True}
    assert "250" not in (core.get_payload(fragment.payload_ref) or "")

    # the certificate names discharged vs retained and cites the source chain
    certificate = core.get_record("certificate:probe:afford")
    assert isinstance(certificate, TransformCertificate)
    assert certificate.registry_transform_id == "FIXED_AFFORDABILITY_PREDICATE"
    assert certificate.parameter_values == (Pair(first="price", second=30),)
    assert [ref.id for ref in certificate.discharged_commitment_refs] == ["commit:finance-strong"]
    assert certificate.retained_commitment_refs == ()
    assert [ref.id for ref in certificate.authorizing_use_refs] == ["use:finance-fields"]
    # the authorized source view persists as the auditable basis, and the
    # production cascade edge exists: revoking the source must reach the
    # transform output view (I14 late derivations, hermes-agora-5e2)
    assert core.get_record("view:probe:afford") is not None
    assert "view:probe:afford" in core.parents_of("view:probe:afford:transform")


def test_field_view_transform_discharges_nothing(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    result = context.request_access(
        make_request(
            request_id="probe:fields",
            intent_ref=intent_ref,
            resource_id=FINANCE_RESOURCE,
            transform_id="FIXED_FIELD_VIEW",
        )
    )
    assert result.access_status == AccessStatus.GRANTED
    assert result.view_kind == ViewKind.PARTIAL  # field values, not a predicate
    assert result.view_ref is not None
    view = core.get_record(result.view_ref.id)
    assert isinstance(view, MemoryView)
    assert view.precision == RequestedPrecision.FIELDS
    # field values still carry the protected content: nothing is discharged
    certificate = core.get_record("certificate:probe:fields")
    assert isinstance(certificate, TransformCertificate)
    assert certificate.discharged_commitment_refs == ()
    assert [ref.id for ref in certificate.retained_commitment_refs] == ["commit:finance-strong"]
    assert [ref.id for ref in view.inherited_commitment_refs] == ["commit:finance-strong"]
    fragment = core.get_record("fragment:transform:probe:fields")
    assert isinstance(fragment, ContentFragment)
    output = json.loads(core.get_payload(fragment.payload_ref) or "{}")
    assert output == {"fields": {"available": 250}}


def test_unauthorized_transform_denied_before_certificate(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    # the third-party policy allows no transforms at all, and no
    # authorization covers TRANSFORM for that resource anyway
    result = context.request_access(
        make_request(
            request_id="probe:bad-transform",
            intent_ref=intent_ref,
            resource_id=THIRD_PARTY_RESOURCE,
            transform_id="FIXED_AFFORDABILITY_PREDICATE",
        )
    )
    assert result.access_status == AccessStatus.DENIED
    assert core.get_record("certificate:probe:bad-transform") is None
    assert core.get_record("fragment:transform:probe:bad-transform") is None


# ---------------------------------------------------------------------------
# Review(section 2) regressions: data-class merging, typed transform failures
# ---------------------------------------------------------------------------


def seed_extra_member(
    core: CoreStore,
    resource_id: str,
    *,
    text: str = '{"available": 80}',
    data_class: str = "financial",
    commitment_id: str | None = None,
) -> None:
    """A complete second registry member (fragment/policy/binding/auth/
    commitment) written directly through the record API."""
    from agora.contracts import (
        ApprovalRule,
        AuthorityBinding,
        AuthorityDomain,
        Commitment,
        CommitmentStrength,
        Principal,
        PrincipalKind,
        RecordStatus,
        RequestedPrecision,
        ResourceOperation,
        ResourcePolicy,
        UseAuthorization,
    )

    now = "2026-10-12T12:00:00Z"
    principal_id = f"principal:issuer:{resource_id.split(':')[-1]}"
    core.put_record(
        Principal(
            id=principal_id,
            revision=1,
            created_at=now,
            kind=PrincipalKind.HUMAN_OPERATOR,
            verified_bindings=(),
            claimed_aliases=(),
        )
    )
    core.put_payload(f"payload:{resource_id}", text, owner_ref=resource_id)
    from agora.contracts import ContentFragment, EligibilityStatus, FragmentKind
    from tests.test_memory_lifecycle import _plain_provenance_for

    core.put_record(
        ContentFragment(
            id=resource_id,
            revision=1,
            created_at=now,
            kind=FragmentKind.TEXT,
            payload_ref=f"payload:{resource_id}",
            provenance=_plain_provenance_for(principal_id),
            commitment_refs=(),
            eligibility_status=EligibilityStatus.ACTIVE,
        )
    )
    commitment_ref = None
    if commitment_id is not None:
        commitment = Commitment(
            id=commitment_id,
            revision=1,
            created_at=now,
            subject_refs=(),
            custodian_refs=(),
            resource_id=resource_id,
            source_scope=None,
            strength=CommitmentStrength.STRONG,
            internal_use_allowed=True,
            observable_influence_allowed=False,
            expression_allowed=False,
            audience_predicate_id="pred:subject-only",
            policy_evidence_refs=(),
        )
        core.put_record(commitment)
        commitment_ref = RecordRef(id=commitment_id, revision=1)
    core.put_records(
        (
            ResourcePolicy(
                id=f"policy:{resource_id}",
                revision=1,
                created_at=now,
                resource_id=resource_id,
                data_class=data_class,
                approval_rule=ApprovalRule.SINGLE_ISSUER,
                eligible_issuer_refs=(RecordRef(id=principal_id, revision=1),),
                commitment_refs=(commitment_ref,) if commitment_ref else (),
                allowed_transform_ids=("FIXED_AFFORDABILITY_PREDICATE",),
                policy_revision=1,
                status=RecordStatus.ACTIVE,
            ),
            AuthorityBinding(
                id=f"binding:{resource_id}",
                revision=1,
                created_at=now,
                issuer_ref=RecordRef(id=principal_id, revision=1),
                domain=AuthorityDomain.RESOURCE_USE,
                resource_id=resource_id,
                evidence_ref=RecordRef(id="fragment:evidence:bindings", revision=1),
                delegated_from=None,
                valid_until=None,
                revoked_at=None,
            ),
            UseAuthorization(
                id=f"use:{resource_id}",
                revision=1,
                created_at=now,
                resource_id=resource_id,
                issuer_refs=(RecordRef(id=principal_id, revision=1),),
                binding_refs=(RecordRef(id=f"binding:{resource_id}", revision=1),),
                operations=(ResourceOperation.READ, ResourceOperation.TRANSFORM),
                permitted_purpose_classes=("social-participation",),
                audience_class_bounds=("room:public",),
                maximum_precision=RequestedPrecision.FIELDS,
                allowed_transform_ids=("FIXED_AFFORDABILITY_PREDICATE",),
                valid_until=None,
                status=RecordStatus.ACTIVE,
            ),
        )
    )


def test_data_class_view_carries_every_members_restrictions(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    seed_extra_member(
        core,
        "fragment:res:finance-2",
        commitment_id="commit:finance-2-strong",
    )
    result = context.request_access(
        make_request(
            request_id="probe:class2",
            intent_ref=intent_ref,
            resource_id=None,
            data_class="financial",
        )
    )
    assert result.access_status == AccessStatus.GRANTED
    assert result.view_ref is not None
    view = core.get_record(result.view_ref.id)
    assert isinstance(view, MemoryView)
    # both members' fragments ship; BOTH members' restrictions ship with them
    assert {ref.id for ref in view.returned_fragment_refs} == {
        FINANCE_RESOURCE,
        "fragment:res:finance-2",
    }
    assert {ref.id for ref in view.inherited_commitment_refs} == {
        "commit:finance-strong",
        "commit:finance-2-strong",
    }
    assert {ref.id for ref in view.authorization_refs} == {
        "use:finance-fields",
        "use:fragment:res:finance-2",
    }


def test_data_class_duplicate_policy_conflicts(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    seed_extra_member(core, "fragment:res:finance-2")
    # a second ACTIVE policy for the SAME resource -> contradictory rules
    from agora.contracts import ApprovalRule, RecordStatus, ResourcePolicy

    duplicate = ResourcePolicy(
        id="policy:finance-duplicate",
        revision=1,
        created_at="2026-10-12T12:00:00Z",
        resource_id=FINANCE_RESOURCE,
        data_class="financial",
        approval_rule=ApprovalRule.SINGLE_ISSUER,
        eligible_issuer_refs=(RecordRef(id="principal:human", revision=1),),
        commitment_refs=(),
        allowed_transform_ids=(),
        policy_revision=1,
        status=RecordStatus.ACTIVE,
    )
    core.put_record(duplicate)
    result = context.request_access(
        make_request(
            request_id="probe:class3",
            intent_ref=intent_ref,
            resource_id=None,
            data_class="financial",
        )
    )
    assert result.access_status == AccessStatus.CONFLICT


def test_transform_run_failure_is_a_typed_denial(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    # prose payload: the affordability predicate cannot run on it
    seed_extra_member(core, "fragment:res:finance-prose", text="just words, no json")
    result = context.request_access(
        make_request(
            request_id="probe:prose",
            intent_ref=intent_ref,
            resource_id="fragment:res:finance-prose",
            transform_id="FIXED_AFFORDABILITY_PREDICATE",
        )
    )
    assert result.access_status == AccessStatus.DENIED
    assert ReasonCode.UNSUPPORTED in result.reason_codes
    assert result.view_ref is None
    # the request is durable; no view/certificate was ever written
    assert core.get_record("request:probe:prose") is not None
    assert core.get_record("view:probe:prose") is None
    assert core.get_record("certificate:probe:prose") is None


def test_denied_replay_audits_once(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    request = make_request(request_id="probe:deny", intent_ref=intent_ref)
    first = context.request_access(request)
    second = context.request_access(request)
    assert first.access_status == AccessStatus.DENIED
    assert second.access_status == AccessStatus.DENIED
    rows = core._conn.execute(
        "SELECT count(*) FROM core_audit WHERE entity_ref = ?", (first.id,)
    ).fetchone()[0]
    assert rows == 1
