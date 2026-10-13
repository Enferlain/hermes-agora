"""M2 task 3.3: strong INTERNAL_ONLY containment at snapshot build.

A view whose permitted uses are internal-only — commitment-backed or
transform-constructed — is structurally inadmissible to an externally
effect-capable snapshot, a DM included. The failure happens at build,
never at dispatch, and no reviewer waives it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agora.context_memory import ContextMemory, ViewAdmissionError
from agora.contracts import (
    AudienceSnapshot,
    Completeness,
    EnvironmentKind,
    HistoryVisibility,
    IntendedUse,
    Intent,
    IntentState,
    ProducerCoverage,
    RecordRef,
    RequestedPrecision,
    Visibility,
)
from tests.fixtures_m2 import FINANCE_RESOURCE, JOINT_RESOURCE
from tests.test_request_access import boot, make_request

PUBLIC_AUDIENCE = RecordRef(id="audience:room:public", revision=1)


def dm_audience_ref(core: object, destination_id: str = "room:dm") -> RecordRef:
    from agora.stores import CoreStore

    assert isinstance(core, CoreStore)
    if core.get_record(f"audience:{destination_id}") is None:
        core.put_record(
            AudienceSnapshot(
                id=f"audience:{destination_id}",
                revision=1,
                created_at="2026-10-12T12:00:00Z",
                destination_id=destination_id,
                environment_kind=EnvironmentKind.ROOM,
                visibility=Visibility.PRIVATE_DM,
                known_principal_refs=(),
                unknown_readers=False,
                history_visibility=HistoryVisibility.DM_PARTICIPANTS,
                completeness=Completeness.OBSERVED,
                disclosure_context_revision=1,
                valid_until=None,
            )
        )
    return RecordRef(id=f"audience:{destination_id}", revision=1)


def build(
    context: ContextMemory,
    intent_ref: RecordRef,
    *,
    call_id: str,
    views: tuple[RecordRef, ...],
    effect_capable: bool = True,
    audience: RecordRef = PUBLIC_AUDIENCE,
):
    return context.build_snapshot(
        identity_ref=RecordRef(id="identity:agent", revision=1),
        intent_ref=intent_ref,
        audience_ref=audience,
        episode_ref=None,
        sink_ref=RecordRef(id="sink:scripted", revision=1),
        effect_capable=effect_capable,
        call_id=call_id,
        active_view_refs=views,
    )


def test_internal_only_view_rejected_from_effect_capable_snapshot(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    result = context.request_access(
        make_request(request_id="probe:fin", intent_ref=intent_ref, resource_id=FINANCE_RESOURCE)
    )
    assert result.view_ref is not None  # the READ-only finance view is internal-only
    with pytest.raises(ViewAdmissionError) as excinfo:
        build(context, intent_ref, call_id="snap:contain", views=(result.view_ref,))
    assert excinfo.value.code.value == "VIEW_FORBIDDEN"
    # failure at build: nothing was persisted for that snapshot
    assert core.get_record("snapshot:snap:contain") is None
    assert core.get_record("coverage:context:snap:contain") is None


def test_dm_destination_is_still_externally_effect_capable(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    audience_ref = dm_audience_ref(core)
    result = context.request_access(
        make_request(request_id="probe:fin", intent_ref=intent_ref, resource_id=FINANCE_RESOURCE)
    )
    assert result.view_ref is not None
    with pytest.raises(ViewAdmissionError) as excinfo:
        build(
            context,
            intent_ref,
            call_id="snap:dm",
            views=(result.view_ref,),
            audience=audience_ref,
        )
    assert excinfo.value.code.value == "VIEW_FORBIDDEN"


def test_internal_only_view_admitted_to_non_effect_snapshot(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    result = context.request_access(
        make_request(request_id="probe:fin", intent_ref=intent_ref, resource_id=FINANCE_RESOURCE)
    )
    assert result.view_ref is not None
    snapshot = build(
        context,
        intent_ref,
        call_id="snap:internal",
        views=(result.view_ref,),
        effect_capable=False,
    )
    assert [ref.id for ref in snapshot.active_view_refs] == [result.view_ref.id]
    # restriction inheritance: the view's STRONG commitment travels with it
    assert [ref.id for ref in snapshot.inherited_commitment_refs] == ["commit:finance-strong"]
    coverage = core.get_record(snapshot.producer_coverage_ref.id)
    assert isinstance(coverage, ProducerCoverage)
    assert [ref.id for ref in coverage.input_view_refs] == [result.view_ref.id]
    assert "snapshot:snap:internal" in core.children_of(result.view_ref.id)


def coordination_intent(core: object, base: RecordRef) -> RecordRef:
    """A project-coordination intent (the joint grant's purpose class)."""
    from agora.stores import CoreStore

    assert isinstance(core, CoreStore)
    record = core.get_record(base.id, base.revision)
    assert isinstance(record, Intent)
    core.put_record(
        Intent(
            id="intent:coordination",
            revision=1,
            created_at="2026-10-12T12:00:00Z",
            creator_ref=record.creator_ref,
            goal_fragment_ref=record.goal_fragment_ref,
            purpose_class="project-coordination",
            observed_trigger_refs=(),
            endorsement_revision=1,
            producer_coverage_ref=None,
            runtime_task_root_ids=("root:coordination",),
            parent_work_ref=None,
            allowed_destination_refs=("room:public",),
            due_at=None,
            expires_at=None,
            state=IntentState.OPEN,
        )
    )
    return RecordRef(id="intent:coordination", revision=1)


def test_view_with_expression_use_admitted_and_inherits_restrictions(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    coordination = coordination_intent(core, intent_ref)
    # the joint grant permits EXPRESS: the view carries an outward use
    result = context.request_access(
        make_request(
            request_id="probe:joint",
            intent_ref=coordination,
            resource_id=JOINT_RESOURCE,
            intended_use=IntendedUse.EXPRESSION,
            precision=RequestedPrecision.ABSTRACT,
        )
    )
    assert result.view_ref is not None
    snapshot = build(context, intent_ref, call_id="snap:express", views=(result.view_ref,))
    assert [ref.id for ref in snapshot.active_view_refs] == [result.view_ref.id]
    # the joint STRONG commitment is inherited into the snapshot for §8 routing
    assert "commit:joint-strong" in [ref.id for ref in snapshot.inherited_commitment_refs]


def test_transform_output_view_is_also_strongly_internal_only(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    result = context.request_access(
        make_request(
            request_id="probe:afford",
            intent_ref=intent_ref,
            resource_id=FINANCE_RESOURCE,
            transform_id="FIXED_AFFORDABILITY_PREDICATE",
        )
    )
    assert result.view_ref is not None
    view = core.get_record(result.view_ref.id)
    from agora.contracts import MemoryView

    assert isinstance(view, MemoryView)
    assert view.permitted_uses == (IntendedUse.INTERNAL_ONLY,)
    with pytest.raises(ViewAdmissionError):
        build(context, intent_ref, call_id="snap:pred", views=(result.view_ref,))


def test_stale_or_inactive_view_fails_admission(tmp_path: Path) -> None:
    core, context, _runner, intent_ref = boot(tmp_path)
    result = context.request_access(
        make_request(request_id="probe:fin", intent_ref=intent_ref, resource_id=FINANCE_RESOURCE)
    )
    assert result.view_ref is not None
    # a second, REVOKED revision makes the cited revision non-current
    from dataclasses import replace

    from agora.contracts import MemoryView, RecordStatus

    view = core.get_record(result.view_ref.id, result.view_ref.revision)
    assert isinstance(view, MemoryView)
    core.put_record(replace(view, revision=2, status=RecordStatus.REVOKED))
    with pytest.raises(ViewAdmissionError) as excinfo:
        build(
            context,
            intent_ref,
            call_id="snap:stale",
            views=(result.view_ref,),
            effect_capable=False,
        )
    assert excinfo.value.code.value == "STALE_CONTEXT"
    with pytest.raises(ViewAdmissionError, match="does not resolve"):
        build(
            context,
            intent_ref,
            call_id="snap:ghost",
            views=(RecordRef(id="view:ghost", revision=1),),
            effect_capable=False,
        )
