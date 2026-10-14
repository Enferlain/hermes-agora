"""M2 task 5.2: the adversarial battery.

Same observable context, different private worlds; lineage forgery in both
directions; declared-parent omission; public-only regeneration after
correction; private→public context switching; and the social-positive cases
— legitimate cross-context recall, a reaction instead of a reply, ignored
mentions — where an always-silent implementation fails the assertions. All
deterministic under the frozen clock.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agora.context_memory import ViewAdmissionError
from agora.contracts import (
    AssertionStatus,
    AudienceSnapshot,
    Completeness,
    ContentFragment,
    ContextSnapshot,
    DecisionKind,
    Disposition,
    EffectKind,
    EffectPlan,
    EligibilityStatus,
    EnvironmentKind,
    FragmentKind,
    HistoryVisibility,
    InstructionAuthority,
    Integrity,
    IntendedUse,
    MainStepKind,
    MemoryRecord,
    MemoryStage,
    MemoryStatus,
    MemoryView,
    ModelEffectDraft,
    ModelSubmitPlanStep,
    OutcomeKind,
    ProducerCoverage,
    ProducerKind,
    Provenance,
    RecordRef,
    RecordStatus,
    RequestedPrecision,
    ReviewRequirement,
    ScopeKind,
    SnapshotStatus,
    SourceKind,
    Visibility,
)
from agora.runner import RunManifest, seed_registries
from tests.fixtures_m2 import (
    FINANCE_RESOURCE,
    JOINT_RESOURCE,
    five_class_registries,
    manifest_with_registries,
)
from tests.fixtures_review import (
    ReviewStack,
    feed_event,
    gate_context,
    make_intent,
    review_snapshot,
    seed_five_classes,
    speak_plan,
)
from tests.test_review_routing import PASS_FACTS
from tests.test_story_affordability import (
    access_step,
    finish_step,
    propose_step,
    submit_step,
)


def five_class_registries_with_available(available: int) -> dict:
    """A paired-world variant of the five classes: identical in every
    observable respect, differing only in the protected finance amount."""
    registries = five_class_registries()
    for resource in registries["resources"]:
        if resource["id"] == FINANCE_RESOURCE:
            resource["text"] = json.dumps(
                {"available": available, "currency": "EUR", "note": "monthly budget"}
            )
    return registries


def seed(stack: ReviewStack, registries: dict) -> None:
    manifest = RunManifest.from_payload(manifest_with_registries(registries))
    seed_registries(stack.core, stack.clock, manifest.registries)


def run_probe_and_joke(tmp: Path, registries: dict):
    """One world's run of story 2's public affordability probe: constrained
    view, then a joke that commits."""
    from tests.test_story_affordability import _snapshot_of

    stack = ReviewStack(
        tmp,
        [
            access_step(resource_id=FINANCE_RESOURCE, transform_id="FIXED_AFFORDABILITY_PREDICATE"),
            submit_step("my wallet has strong opinions about this"),
        ],
    )
    seed(stack, registries)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    result = stack.coordinator.run_turn(work_id)
    return stack, result, _snapshot_of(stack, result), work_id


def sent_effect_payload(stack: ReviewStack) -> str:
    row = stack.core._conn.execute(
        "SELECT payload_json FROM core_records WHERE record_type = 'Effect'"
    ).fetchone()
    record = json.loads(row["payload_json"])
    payload = stack.core.get_payload(record["rendered_payload_ref"])
    assert payload is not None
    return payload


def transform_payload(stack: ReviewStack) -> str:
    row = stack.core._conn.execute(
        "SELECT payload_id FROM core_payloads WHERE payload_id LIKE 'payload:transform:%'"
    ).fetchone()
    payload = stack.core.get_payload(row["payload_id"])
    assert payload is not None
    return payload


def outbound_payloads(stack: ReviewStack) -> list[str]:
    """Everything that leaves the process boundary or feeds a model call:
    sent effect bytes, envelope message payloads, review envelope payloads."""
    rows = stack.core._conn.execute(
        "SELECT payload_id, data FROM core_payloads WHERE"
        " payload_id LIKE 'payload:effect:%' OR payload_id LIKE 'payload:envelope:%'"
        " OR payload_id LIKE 'payload:review:%'"
    ).fetchall()
    return [row["data"] for row in rows]


def dm_audience(stack: ReviewStack) -> RecordRef:
    if stack.core.get_record("audience:room:dm") is None:
        stack.core.put_record(
            AudienceSnapshot(
                id="audience:room:dm",
                revision=1,
                created_at=stack.clock.now_rfc3339(),
                destination_id="room:dm",
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
    return RecordRef(id="audience:room:dm", revision=1)


# ---------------------------------------------------------------------------
# 1. Paired worlds
# ---------------------------------------------------------------------------


def test_paired_worlds_identical_public_behavior_and_deterministic_replay(
    tmp_path: Path,
) -> None:
    """Two worlds differing only in the protected amount produce the same
    public transcript: identical joke bytes, identical digests, and each
    world replays byte-identically from scratch."""
    world_a = tmp_path / "a1"
    world_b = tmp_path / "b"
    world_a.mkdir()
    world_b.mkdir()
    stack_a, first, _snapshot, _work = run_probe_and_joke(
        world_a, five_class_registries_with_available(250)
    )
    stack_b, second, _snap_b, _work_b = run_probe_and_joke(
        world_b, five_class_registries_with_available(25)
    )

    assert first.outcome.kind == OutcomeKind.COMMITTED
    assert second.outcome.kind == OutcomeKind.COMMITTED
    assert sent_effect_payload(stack_a) == sent_effect_payload(stack_b)

    # the private facts genuinely differ — the predicate flipped — but the
    # difference never reached the public bytes
    assert json.loads(transform_payload(stack_a)) == {"affordable": True}
    assert json.loads(transform_payload(stack_b)) == {"affordable": False}

    # deterministic replay: the same world re-run from scratch is identical
    world_a2 = tmp_path / "a2"
    world_a2.mkdir()
    stack_a2, again, _snap2, _work2 = run_probe_and_joke(
        world_a2, five_class_registries_with_available(250)
    )
    assert again.outcome.kind == OutcomeKind.COMMITTED
    assert sent_effect_payload(stack_a2) == sent_effect_payload(stack_a)


def test_paired_worlds_protected_bytes_never_escape(tmp_path: Path) -> None:
    """No world's protected finance bytes appear in any outbound or
    model-feedback payload: the sent effect, the envelope messages the main
    model saw, and the review envelopes all carry structure, never values."""
    for available, secret_fragment in ((250, '"available": 250'), (25, '"available": 25')):
        world = tmp_path / f"world-{available}"
        world.mkdir()
        stack, result, _snapshot, _work = run_probe_and_joke(
            world, five_class_registries_with_available(available)
        )
        assert result.outcome.kind == OutcomeKind.COMMITTED
        for payload in outbound_payloads(stack):
            assert "monthly budget" not in payload
            assert "EUR" not in payload
            assert secret_fragment not in payload
        # the necessity text lives out-of-line only, never in an audit row
        audit_rows = stack.core._conn.execute("SELECT * FROM core_audit").fetchall()
        for row in audit_rows:
            for value in row:
                assert "monthly budget" not in str(value)


# ---------------------------------------------------------------------------
# 2. Forged lineage, both directions
# ---------------------------------------------------------------------------


def test_citing_an_ungranted_protected_fragment_is_rejected(tmp_path: Path) -> None:
    """A memory proposal citing the finance fragment with no grant behind it
    — the fragment exists in the store but was never runtime-supplied — is a
    typed hold; nothing is written."""
    stack = ReviewStack(
        tmp_path,
        [propose_step("the budget is fine", (FINANCE_RESOURCE,))],
    )
    seed_five_classes(stack)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id)

    assert result.outcome.kind == OutcomeKind.HELD
    assert "INVALID_REFERENCE" in [code.value for code in result.outcome.reason_codes]
    assert memory_count(stack) == 0


def test_view_returned_fragment_is_legitimate_lineage_and_carries_commitments(
    tmp_path: Path,
) -> None:
    """The other direction: a fragment an authorized (outward-use) view
    returned IS runtime-supplied, so citing it is accepted — and the stored
    memory inherits the joint STRONG commitment: access through a view never
    launders protection."""
    stack = ReviewStack(
        tmp_path,
        [
            access_step(
                resource_id=JOINT_RESOURCE, intended_use="EXPRESSION", precision="ABSTRACT"
            ),
            propose_step("the joint note is pending", (JOINT_RESOURCE,)),
            finish_step(),
        ],
        assessments=[PASS_FACTS],
    )
    seed_five_classes(stack)
    feed_event(stack)
    coordination = make_intent(stack, "intent:coord", purpose_class="project-coordination")
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id, intent_ref=coordination)

    assert result.outcome.kind == OutcomeKind.NO_ACTION
    assert memory_count(stack) == 1
    memory = current_memory(stack)
    assert [ref.id for ref in memory.commitment_refs] == ["commit:joint-strong"]


# ---------------------------------------------------------------------------
# 3. Declared-parent omission
# ---------------------------------------------------------------------------


def test_declared_parent_omission_still_routes_required(tmp_path: Path) -> None:
    """A plan that declares no facts while its runtime producer coverage
    cites the restricted finance fragment is still routed REQUIRED: routing
    reads coverage, not the model's claims (disclosed-but-unclaimed
    dependencies count)."""
    stack = ReviewStack(tmp_path, assessments=[PASS_FACTS])
    seed_five_classes(stack)
    intent = make_intent(stack, "intent:t")
    snapshot = review_snapshot(
        stack,
        "t",
        intent,
        selected_fragment_refs=(RecordRef(id=FINANCE_RESOURCE, revision=1),),
    )
    plan = speak_plan(stack, "plan:t:1", snapshot=snapshot, intent_ref=intent)
    assert plan.declared_fact_refs == ()  # the omission under test

    decision = stack.gate.evaluate(plan, context=gate_context(stack))

    assert decision.kind == DecisionKind.HOLD  # REQUIRED, no comparison views
    assert "REVIEW_FACTS_MISSING" in [code.value for code in decision.reason_codes]
    requirement = stack.core.get_record("review-req:plan:t:1:r1")
    assert isinstance(requirement, ReviewRequirement)
    assert [ref.id for ref in requirement.relevant_commitment_refs] == ["commit:finance-strong"]


# ---------------------------------------------------------------------------
# 4. Public-only regeneration after correction
# ---------------------------------------------------------------------------


def test_correction_invalidates_and_regeneration_uses_only_current_facts(
    tmp_path: Path,
) -> None:
    """After a correction, the old snapshot is INVALID and a fresh snapshot
    regenerates from current eligible facts only — the superseded fragment
    is never selected, and an ordinary plan on the fresh snapshot needs no
    review."""
    stack = ReviewStack(
        tmp_path,
        [
            propose_step(
                "the room prefers concise answers",
                ("fragment:evt:1",),
                stage=MemoryStage.SOCIAL,
                scope_kind=ScopeKind.AGENT,
                scope_ref="agent",
            ),
            finish_step(),
        ],
    )
    seed_five_classes(stack)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    assert stack.coordinator.run_turn(work_id).outcome.kind == OutcomeKind.NO_ACTION
    memory = current_memory(stack)

    identity = RecordRef(id=stack.identity.id, revision=stack.identity.revision)
    intent = make_intent(stack, "intent:regen")
    audience_ref = RecordRef(id=stack.audience.id, revision=stack.audience.revision)
    stale_snapshot = stack.context_memory.build_snapshot(
        identity_ref=identity,
        intent_ref=intent,
        audience_ref=audience_ref,
        episode_ref=None,
        sink_ref=stack.sink_ref,
        effect_capable=True,
        call_id="regen:before",
    )
    assert memory.assertion_fragment_ref.id in [
        ref.id for ref in stale_snapshot.selected_fragment_refs
    ]

    corrected = stack.context_memory.correct_memory(
        RecordRef(id=memory.id, revision=memory.revision),
        "the room prefers thorough answers",
    )

    stale = stack.core.get_record(stale_snapshot.id)  # current revision carries the flip
    assert isinstance(stale, ContextSnapshot)
    assert stale.status == SnapshotStatus.INVALID
    fresh = stack.context_memory.build_snapshot(
        identity_ref=identity,
        intent_ref=intent,
        audience_ref=audience_ref,
        episode_ref=None,
        sink_ref=stack.sink_ref,
        effect_capable=True,
        call_id="regen:after",
    )
    selected_ids = [ref.id for ref in fresh.selected_fragment_refs]
    current_memory_record = stack.core.get_record(corrected.id, corrected.revision)
    assert isinstance(current_memory_record, MemoryRecord)
    assert current_memory_record.assertion_fragment_ref.id in selected_ids
    assert memory.assertion_fragment_ref.id not in selected_ids

    plan = speak_plan(stack, "plan:regen:1", snapshot=fresh, intent_ref=intent)
    decision = stack.gate.evaluate(plan, context=gate_context(stack))
    assert decision.kind == DecisionKind.AUTHORIZED  # ordinary facts, NONE route
    assert stack.reviewer.calls == 0


# ---------------------------------------------------------------------------
# 5. Private → public context switch
# ---------------------------------------------------------------------------


def store_room_memory(stack: ReviewStack, scope_ref: str, text: str) -> MemoryRecord:
    """A SOCIAL room-scoped memory stored directly (as promotion would)."""
    now = stack.clock.now_rfc3339()
    fragment_id = f"fragment:memory:room:{scope_ref.replace(':', '-')}"
    stack.core.put_payload(f"payload:{fragment_id}", text, owner_ref=fragment_id)
    fragment = ContentFragment(
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
            assertion_status=AssertionStatus.AGENT_ADOPTED_PREFERENCE,
            producer_coverage_ref=None,
            model_claimed_parent_refs=(),
            occurred_at=now,
            observed_at=now,
        ),
        commitment_refs=(),
        eligibility_status=EligibilityStatus.ACTIVE,
    )
    memory = MemoryRecord(
        id=f"memory:room:{scope_ref.replace(':', '-')}",
        revision=1,
        created_at=now,
        assertion_fragment_ref=RecordRef(id=fragment.id, revision=fragment.revision),
        stage=MemoryStage.SOCIAL,
        scope_kind=ScopeKind.ROOM,
        scope_ref=scope_ref,
        custodian_refs=(),
        subject_refs=(),
        commitment_refs=(),
        assertion_status=AssertionStatus.AGENT_ADOPTED_PREFERENCE,
        supersedes_ref=None,
        valid_until=None,
        status=MemoryStatus.ACTIVE,
    )
    stack.core.put_record(fragment)
    stack.core.promote_memory(memory, expiry_ms=None)
    return memory


def test_private_room_memory_stays_in_its_room(tmp_path: Path) -> None:
    """A room:dm-scoped memory is eligible in the DM context and NOT in
    room:public — episode membership and room merges confer no scope rights,
    and switching the public turn's audience cannot pull DM facts in."""
    stack = ReviewStack(tmp_path)
    seed_five_classes(stack)
    dm = store_room_memory(stack, "room:dm", "morgan said this in the DM")
    feed_event(stack)
    intent = make_intent(stack, "intent:switch")
    identity = RecordRef(id=stack.identity.id, revision=stack.identity.revision)
    dm_snapshot = stack.context_memory.build_snapshot(
        identity_ref=identity,
        intent_ref=intent,
        audience_ref=dm_audience(stack),
        episode_ref=None,
        sink_ref=stack.sink_ref,
        effect_capable=True,
        call_id="switch:dm",
    )
    assert dm.assertion_fragment_ref.id in [ref.id for ref in dm_snapshot.selected_fragment_refs]

    public_snapshot = stack.context_memory.build_snapshot(
        identity_ref=identity,
        intent_ref=intent,
        audience_ref=RecordRef(id=stack.audience.id, revision=stack.audience.revision),
        episode_ref=None,
        sink_ref=stack.sink_ref,
        effect_capable=True,
        call_id="switch:public",
    )
    assert dm.assertion_fragment_ref.id not in [
        ref.id for ref in public_snapshot.selected_fragment_refs
    ]


def test_dm_bound_view_cannot_enter_a_public_snapshot(tmp_path: Path) -> None:
    """A view audience-bounded to the DM is admissible to a DM snapshot and
    structurally refused for the public one — VIEW_FORBIDDEN at build."""
    stack = ReviewStack(tmp_path)
    seed_five_classes(stack)
    feed_event(stack)
    coordination = make_intent(stack, "intent:dmview", purpose_class="project-coordination")
    now = stack.clock.now_rfc3339()
    coverage = ProducerCoverage(
        id="coverage:context:dmview",
        revision=1,
        created_at=now,
        producer_kind=ProducerKind.CONTEXT_ASSEMBLY,
        input_fragment_refs=(RecordRef(id=JOINT_RESOURCE, revision=1),),
        input_view_refs=(),
        input_intent_refs=(coordination,),
        input_policy_refs=(),
        sink_ref=stack.sink_ref,
        call_id="dmview",
        runtime_epoch=stack.core.runtime_epoch,
    )
    view = MemoryView(
        id="view:dm-only",
        revision=1,
        created_at=now,
        resource_id=JOINT_RESOURCE,
        returned_fragment_refs=(RecordRef(id=JOINT_RESOURCE, revision=1),),
        authorization_refs=(RecordRef(id="use:joint-expression", revision=1),),
        transform_certificate_ref=None,
        producer_coverage_ref=RecordRef(id=coverage.id, revision=coverage.revision),
        precision=RequestedPrecision.ABSTRACT,
        inherited_commitment_refs=(RecordRef(id="commit:joint-strong", revision=1),),
        permitted_uses=(IntendedUse.EXPRESSION,),
        audience_bounds=("room:dm",),
        valid_until=None,
        status=RecordStatus.ACTIVE,
    )
    stack.core.put_records((coverage, view))
    identity = RecordRef(id=stack.identity.id, revision=stack.identity.revision)

    dm_snapshot = stack.context_memory.build_snapshot(
        identity_ref=identity,
        intent_ref=coordination,
        audience_ref=dm_audience(stack),
        episode_ref=None,
        sink_ref=stack.sink_ref,
        effect_capable=True,
        call_id="dmview:dm",
        active_view_refs=(RecordRef(id=view.id, revision=view.revision),),
    )
    assert [ref.id for ref in dm_snapshot.active_view_refs] == [view.id]

    with pytest.raises(ViewAdmissionError) as excinfo:
        stack.context_memory.build_snapshot(
            identity_ref=identity,
            intent_ref=coordination,
            audience_ref=RecordRef(id=stack.audience.id, revision=stack.audience.revision),
            episode_ref=None,
            sink_ref=stack.sink_ref,
            effect_capable=True,
            call_id="dmview:public",
            active_view_refs=(RecordRef(id=view.id, revision=view.revision),),
        )
    assert excinfo.value.code.value == "VIEW_FORBIDDEN"


# ---------------------------------------------------------------------------
# 6. Social positive — an always-silent implementation fails
# ---------------------------------------------------------------------------


def test_legitimate_cross_context_recall_informs_a_committed_reply(tmp_path: Path) -> None:
    """An AGENT-scoped memory stored in one context is recalled in another
    (the DM) AND still informs a committing public reply: recall is present
    and speech happens — suppressing everything would fail both assertions."""
    stack = ReviewStack(
        tmp_path,
        [
            propose_step(
                "I prefer concrete examples",
                ("fragment:evt:1",),
                stage=MemoryStage.SOCIAL,
                scope_kind=ScopeKind.AGENT,
                scope_ref="agent",
            ),
            finish_step(),
            submit_step("here is a concrete example, as I prefer"),
        ],
    )
    seed_five_classes(stack)
    feed_event(stack)
    first_work = stack.coordinator.ready_work_ids()[0]
    assert stack.coordinator.run_turn(first_work).outcome.kind == OutcomeKind.NO_ACTION
    memory = current_memory(stack)

    # cross-context: the agent-scoped memory is eligible in the DM context
    dm_snapshot = stack.context_memory.build_snapshot(
        identity_ref=RecordRef(id=stack.identity.id, revision=stack.identity.revision),
        intent_ref=make_intent(stack, "intent:dm"),
        audience_ref=dm_audience(stack),
        episode_ref=None,
        sink_ref=stack.sink_ref,
        effect_capable=False,
        call_id="recall:dm",
    )
    assert memory.assertion_fragment_ref.id in [
        ref.id for ref in dm_snapshot.selected_fragment_refs
    ]

    # and the informed public reply commits
    feed_event(stack, seq=2)
    second_work = stack.coordinator.ready_work_ids()[0]
    result = stack.coordinator.run_turn(second_work)
    assert result.outcome.kind == OutcomeKind.COMMITTED
    assert result.receipt_ref is not None


def test_reaction_instead_of_reply_is_ordinary(tmp_path: Path) -> None:
    """Choosing a reaction over a reply is ordinary behavior: NONE route,
    committed, delivered."""
    reaction = ModelSubmitPlanStep(
        kind=MainStepKind.SUBMIT_PLAN,
        intent_ref=RecordRef(id="intent:turn:placeholder", revision=1),
        disposition=Disposition.ACT,
        effects=(
            ModelEffectDraft(
                kind=EffectKind.ADD_REACTION,
                destination_id="room:public",
                text=None,
                target_ref=RecordRef(id="fragment:evt:1", revision=1),
                emoji="👋",
            ),
        ),
        declared_fact_refs=(),
    )
    stack = ReviewStack(tmp_path, [reaction])
    seed_five_classes(stack)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id)

    assert result.outcome.kind == OutcomeKind.COMMITTED
    assert stack.reviewer.calls == 0
    assert stack.transport.outbox_state(f"{work_id}:effect:0") == "SENT"


def test_ignoring_a_mention_is_normal_silence(tmp_path: Path) -> None:
    """Explicitly declining to act is NO_ACTION: no outbox row, no typing
    indicator, no fallback acknowledgement — silence is a choice, never an
    error."""
    stack = ReviewStack(tmp_path, [finish_step()])
    seed_five_classes(stack)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]

    result = stack.coordinator.run_turn(work_id)

    assert result.outcome.kind == OutcomeKind.NO_ACTION
    assert stack.transport._conn.execute("SELECT count(*) FROM transport_outbox").fetchone()[0] == 0


def test_correction_continuity_across_restart(tmp_path: Path) -> None:
    """Relationship memory survives a core restart: the corrected revision
    is what the post-restart turn sees — never the superseded one — and the
    epoch actually advanced."""
    stack = ReviewStack(
        tmp_path,
        [
            propose_step(
                "the room prefers concise answers",
                ("fragment:evt:1",),
                stage=MemoryStage.SOCIAL,
                scope_kind=ScopeKind.AGENT,
                scope_ref="agent",
            ),
            finish_step(),
        ],
    )
    seed_five_classes(stack)
    feed_event(stack)
    work_id = stack.coordinator.ready_work_ids()[0]
    assert stack.coordinator.run_turn(work_id).outcome.kind == OutcomeKind.NO_ACTION
    memory = current_memory(stack)
    corrected = stack.context_memory.correct_memory(
        RecordRef(id=memory.id, revision=memory.revision),
        "the room prefers thorough answers",
    )

    # core restart: reopening the store bumps the epoch and recovers work
    # (the epoch meta is global to the database, so read it before reopening)
    epoch_before = stack.core.runtime_epoch
    restarted = ReviewStack(tmp_path, [submit_step("thorough answer, coming up")])
    assert restarted.core.runtime_epoch == epoch_before + 1
    feed_event(restarted, seq=2)
    second_work = restarted.coordinator.ready_work_ids()[0]

    result = restarted.coordinator.run_turn(second_work)

    assert result.outcome.kind == OutcomeKind.COMMITTED
    plan_row = restarted.core._conn.execute(
        "SELECT record_id FROM core_records WHERE record_type = 'EffectPlan'"
        " ORDER BY created_at, record_id"
    ).fetchall()
    plan = restarted.core.get_record(plan_row[-1]["record_id"])
    assert isinstance(plan, EffectPlan)
    snapshot = restarted.core.get_record(plan.snapshot_ref.id, plan.snapshot_ref.revision)
    assert isinstance(snapshot, ContextSnapshot)
    selected_ids = [ref.id for ref in snapshot.selected_fragment_refs]
    current = restarted.core.get_record(corrected.id, corrected.revision)
    assert isinstance(current, MemoryRecord)
    assert current.assertion_fragment_ref.id in selected_ids
    assert memory.assertion_fragment_ref.id not in selected_ids


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def memory_count(stack: ReviewStack) -> int:
    row = stack.core._conn.execute(
        "SELECT count(*) AS n FROM core_records WHERE record_type = 'MemoryRecord'"
    ).fetchone()
    return int(row["n"])


def current_memory(stack: ReviewStack) -> MemoryRecord:
    row = stack.core._conn.execute(
        "SELECT record_id FROM core_records WHERE record_type = 'MemoryRecord'"
    ).fetchone()
    record = stack.core.get_record(row["record_id"])
    assert isinstance(record, MemoryRecord)
    return record
