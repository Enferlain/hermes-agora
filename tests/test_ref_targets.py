"""Typed-reference target verification (§7 core.records 'typed refs').

Two layers: the codec-derived drift test keeps REF_TARGETS aligned with the
actual record fields, and the store tests pin the insert-time behavior —
wrong type or missing target fails with nothing written, batches may carry
intra-batch forward/circular references, and nullable refs are skipped.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any, get_type_hints

import pytest

from agora.contracts import (
    ALL_RECORD_TYPES,
    ANY_TARGET,
    PROVENANCE_REF_TARGETS,
    RECORD_TYPES,
    REF_TARGETS,
    RUNTIME_ORIGIN_REF_TARGETS,
    AssertionStatus,
    InstructionAuthority,
    Integrity,
    OutcomeKind,
    Principal,
    PrincipalKind,
    Provenance,
    RecordRef,
    SourceKind,
)
from agora.contracts.codec import _CODEC_REF, _CODEC_SEQ, _annotation_codec, _field_plan
from agora.contracts.records import Record
from agora.contracts.values import Provenance as ProvenanceValue
from agora.contracts.values import RuntimeOrigin as RuntimeOriginValue
from agora.stores import CoreStore, InvalidReferenceError

ANCHOR = "2026-10-12T12:00:00Z"


# ---------------------------------------------------------------------------
# Codec-derived drift test: the map cannot fall out of sync with the records
# ---------------------------------------------------------------------------


def _ref_fields_from_plan(plan: dict[str, tuple[str, Any]]) -> set[str]:
    result: set[str] = set()
    for name, (kind, param) in plan.items():
        if kind == _CODEC_REF or (kind == _CODEC_SEQ and param[0] == _CODEC_REF):
            result.add(name)
    return result


@pytest.mark.parametrize("cls", ALL_RECORD_TYPES, ids=lambda cls: cls.TYPE_NAME)
def test_ref_targets_cover_exactly_the_ref_fields(cls: type[Record]) -> None:
    assert set(REF_TARGETS.get(cls, {})) == _ref_fields_from_plan(_field_plan(cls))


def test_ref_targets_name_only_real_record_types() -> None:
    for targets in REF_TARGETS.values():
        for allowed in targets.values():
            names = {allowed} if isinstance(allowed, str) else set(allowed)
            for name in names:
                assert name == ANY_TARGET or name in RECORD_TYPES, name


def test_ref_targets_hold_no_orphan_classes() -> None:
    assert set(REF_TARGETS) <= set(ALL_RECORD_TYPES)


@pytest.mark.parametrize(
    "value_cls,targets",
    [
        (ProvenanceValue, PROVENANCE_REF_TARGETS),
        (RuntimeOriginValue, RUNTIME_ORIGIN_REF_TARGETS),
    ],
    ids=["provenance", "runtime_origin"],
)
def test_value_object_targets_cover_exactly_their_ref_fields(
    value_cls: type[ProvenanceValue] | type[RuntimeOriginValue], targets: dict[str, Any]
) -> None:
    hints = get_type_hints(value_cls)
    discovered = {
        field_info.name
        for field_info in fields(value_cls)
        if (
            lambda kind, param: (
                kind == _CODEC_REF or (kind == _CODEC_SEQ and param[0] == _CODEC_REF)
            )
        )(*_annotation_codec(hints[field_info.name]))
    }
    assert set(targets) == discovered


# ---------------------------------------------------------------------------
# Store-level verification
# ---------------------------------------------------------------------------


def make_principal(core: CoreStore, principal_id: str = "principal:agent") -> RecordRef:
    core.put_record(
        Principal(
            id=principal_id,
            revision=1,
            created_at=ANCHOR,
            kind=PrincipalKind.AGENT,
            verified_bindings=(),
            claimed_aliases=(),
        )
    )
    return RecordRef(id=principal_id, revision=1)


def make_fragment(core: CoreStore, fragment_id: str) -> RecordRef:
    from agora.contracts import ContentFragment, EligibilityStatus, FragmentKind

    core.put_payload(f"payload:{fragment_id}", "text", owner_ref=fragment_id)
    core.put_record(
        ContentFragment(
            id=fragment_id,
            revision=1,
            created_at=ANCHOR,
            kind=FragmentKind.TEXT,
            payload_ref=f"payload:{fragment_id}",
            provenance=Provenance(
                producer_ref=make_principal(core),
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
    return RecordRef(id=fragment_id, revision=1)


def make_intent(core: CoreStore, intent_id: str) -> RecordRef:
    from agora.contracts import Intent, IntentState

    fragment_ref = make_fragment(core, f"fragment:goal:{intent_id}")
    core.put_record(
        Intent(
            id=intent_id,
            revision=1,
            created_at=ANCHOR,
            creator_ref=make_principal(core),
            goal_fragment_ref=fragment_ref,
            purpose_class="social-participation",
            observed_trigger_refs=(),
            endorsement_revision=1,
            producer_coverage_ref=None,
            runtime_task_root_ids=("root:1",),
            parent_work_ref=None,
            allowed_destination_refs=("room:public",),
            due_at=None,
            expires_at=None,
            state=IntentState.OPEN,
        )
    )
    return RecordRef(id=intent_id, revision=1)


def make_work_item(core: CoreStore, work_id: str, intent_ref: RecordRef) -> RecordRef:
    from agora.contracts import WorkItem, WorkState

    core.put_record(
        WorkItem(
            id=work_id,
            revision=1,
            created_at=ANCHOR,
            intent_ref=intent_ref,
            trigger_key=f"turn:{work_id}",
            episode_ref=None,
            state=WorkState.READY,
            lease_owner=None,
            lease_until_ms=None,
            runtime_epoch=core.runtime_epoch,
            main_steps_used=0,
            submissions_used=0,
            terminal_outcome_ref=None,
        )
    )
    return RecordRef(id=work_id, revision=1)


def make_turn_outcome(core: CoreStore, outcome_id: str, work_ref: RecordRef) -> Any:
    from agora.contracts import ReasonCode, TurnOutcome

    outcome = TurnOutcome(
        id=outcome_id,
        revision=1,
        created_at=ANCHOR,
        kind=OutcomeKind.NO_ACTION,
        intent_ref=None,
        work_ref=work_ref,
        reason_codes=(ReasonCode.NO_ACTION,),
        effect_refs=(),
    )
    return outcome


def test_wrong_target_type_fails_with_nothing_written(tmp_path: Path) -> None:
    core = CoreStore(tmp_path / "core.sqlite")
    principal_ref = make_principal(core)
    # An AgentIdentity whose principal_ref points at... another Principal is
    # fine; point it at an AudienceSnapshot-shaped id instead by first
    # creating a non-Principal record and citing it.
    fragment_ref = make_fragment(core, "fragment:x")
    from agora.contracts import AgentIdentity

    with pytest.raises(InvalidReferenceError) as excinfo:
        core.put_record(
            AgentIdentity(
                id="identity:bad",
                revision=1,
                created_at=ANCHOR,
                principal_ref=fragment_ref,  # a ContentFragment, not a Principal
                preference_refs=(),
                autobiography_refs=(),
                commitment_refs=(),
                active_interest_refs=(),
            )
        )
    assert excinfo.value.reason_code.value == "INVALID_REFERENCE"
    assert "AgentIdentity.principal_ref" in str(excinfo.value)
    assert core.get_record("identity:bad") is None
    assert principal_ref is not None


def test_missing_target_fails_with_nothing_written(tmp_path: Path) -> None:
    core = CoreStore(tmp_path / "core.sqlite")
    make_principal(core)
    from agora.contracts import AgentIdentity

    with pytest.raises(InvalidReferenceError, match="does not exist"):
        core.put_record(
            AgentIdentity(
                id="identity:ghost",
                revision=1,
                created_at=ANCHOR,
                principal_ref=RecordRef(id="principal:ghost", revision=1),
                preference_refs=(),
                autobiography_refs=(),
                commitment_refs=(),
                active_interest_refs=(),
            )
        )
    assert core.get_record("identity:ghost") is None


def test_revision_mismatch_is_a_missing_target(tmp_path: Path) -> None:
    core = CoreStore(tmp_path / "core.sqlite")
    make_principal(core)
    from agora.contracts import AgentIdentity

    with pytest.raises(InvalidReferenceError, match="does not exist"):
        core.put_record(
            AgentIdentity(
                id="identity:rev",
                revision=1,
                created_at=ANCHOR,
                principal_ref=RecordRef(id="principal:agent", revision=7),
                preference_refs=(),
                autobiography_refs=(),
                commitment_refs=(),
                active_interest_refs=(),
            )
        )


def test_nullable_refs_and_empty_tuples_skip_verification(tmp_path: Path) -> None:
    from agora.contracts import ComputeSink, SinkKind, SinkStatus

    core = CoreStore(tmp_path / "core.sqlite")
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
    assert core.get_record("sink:scripted") is not None


def test_stale_revision_of_existing_record_is_accepted_for_vectors(tmp_path: Path) -> None:
    """Historical revisions stay addressable: version vectors and dependency
    edges cite the revision they captured, not the current one."""
    core = CoreStore(tmp_path / "core.sqlite")
    principal_ref = make_principal(core)
    from agora.contracts import AgentIdentity

    core.put_record(
        AgentIdentity(
            id="identity:agent",
            revision=1,
            created_at=ANCHOR,
            principal_ref=principal_ref,
            preference_refs=(),
            autobiography_refs=(),
            commitment_refs=(),
            active_interest_refs=(),
        )
    )
    # A second revision of the identity exists; a ref to revision 1 still
    # resolves because history is append-only.
    first = core.get_record("identity:agent")
    assert first is not None
    core.put_record(
        AgentIdentity(
            id="identity:agent",
            revision=2,
            created_at=ANCHOR,
            principal_ref=principal_ref,
            preference_refs=(),
            autobiography_refs=(),
            commitment_refs=(),
            active_interest_refs=(),
        )
    )
    assert core.get_record("identity:agent", 1) is not None


def test_put_record_rejects_forward_ref_but_put_records_accepts_it(tmp_path: Path) -> None:
    """WorkItem ↔ TurnOutcome is the circular pair: single inserts must fail
    (the counterpart does not exist yet), one batch succeeds."""
    core = CoreStore(tmp_path / "core.sqlite")
    intent_ref = make_intent(core, "intent:1")
    work_ref = RecordRef(id="work:1", revision=1)
    from agora.contracts import WorkItem, WorkState

    work = WorkItem(
        id="work:1",
        revision=1,
        created_at=ANCHOR,
        intent_ref=intent_ref,
        trigger_key="turn:evt:1",
        episode_ref=None,
        state=WorkState.READY,
        lease_owner=None,
        lease_until_ms=None,
        runtime_epoch=core.runtime_epoch,
        main_steps_used=0,
        submissions_used=0,
        terminal_outcome_ref=RecordRef(id="outcome:1", revision=1),
    )
    outcome = make_turn_outcome(core, "outcome:1", work_ref)
    with pytest.raises(InvalidReferenceError, match="does not exist"):
        core.put_record(work)
    assert core.get_record("work:1") is None

    core.put_records((work, outcome))
    assert core.get_record("work:1") is not None
    assert core.get_record("outcome:1") is not None


def test_put_records_bad_ref_rolls_back_whole_batch(tmp_path: Path) -> None:
    core = CoreStore(tmp_path / "core.sqlite")
    intent_ref = make_intent(core, "intent:1")
    from agora.contracts import TurnOutcome, WorkItem, WorkState

    work = WorkItem(
        id="work:2",
        revision=1,
        created_at=ANCHOR,
        intent_ref=intent_ref,
        trigger_key="turn:evt:2",
        episode_ref=None,
        state=WorkState.READY,
        lease_owner=None,
        lease_until_ms=None,
        runtime_epoch=core.runtime_epoch,
        main_steps_used=0,
        submissions_used=0,
        terminal_outcome_ref=RecordRef(id="outcome:2", revision=1),
    )
    outcome = TurnOutcome(
        id="outcome:2",
        revision=1,
        created_at=ANCHOR,
        kind=OutcomeKind.NO_ACTION,
        intent_ref=None,
        work_ref=RecordRef(id="work:2", revision=1),
        reason_codes=(),
        effect_refs=(RecordRef(id="effect:missing", revision=1),),  # dangling
    )
    with pytest.raises(InvalidReferenceError):
        core.put_records((work, outcome))
    assert core.get_record("work:2") is None
    assert core.get_record("outcome:2") is None


def test_verify_reference_resolves_for_coordinator_prechecks(tmp_path: Path) -> None:
    core = CoreStore(tmp_path / "core.sqlite")
    fragment_ref = make_fragment(core, "fragment:ok")
    core.verify_reference(fragment_ref, "ContentFragment", label="model step")
    principal_ref = make_principal(core, "principal:p2")
    with pytest.raises(InvalidReferenceError, match="expected"):
        core.verify_reference(principal_ref, "ContentFragment", label="model step")
    with pytest.raises(InvalidReferenceError, match="does not exist"):
        core.verify_reference(
            RecordRef(id="fragment:nope", revision=1), "ContentFragment", label="model step"
        )
