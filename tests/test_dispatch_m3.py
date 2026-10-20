"""M3 dispatch and invalidation (beads hermes-agora-usf.1, tasks 1.1 through 1.6).

Pre-dispatch revalidation inside the fenced reserve/consume window
(I08/I10), acknowledged supersession (I09), material audience change
(D3), release limits on the M1 reservation ledger (I15/D4), retry and
reconciliation rules (I11), and correction/revocation invalidation of
not-yet-dispatched effects (I14). All drives are deterministic at the
sidecar/gate API level: authorize, mutate the world, submit.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from agora.contracts import (
    AudienceSnapshot,
    CapabilityEnvelope,
    Commitment,
    CommitmentStrength,
    ContentFragment,
    DecisionKind,
    Disposition,
    Effect,
    EffectKind,
    EffectPlan,
    Pair,
    ReasonCode,
    RecordRef,
    RecordStatus,
    SnapshotStatus,
    canonical_digest,
    canonical_json_bytes,
    render_add_reaction,
    render_send_message,
)
from agora.fake_sidecar import ReconcileResult, SidecarError, WorldOutcome
from agora.ports import CrashCheckpoint, InjectedCrash
from tests.fixtures_review import ReviewStack, gate_context, make_intent, review_snapshot

# -- helpers ----------------------------------------------------------------


def _commitment(
    stack: ReviewStack,
    commitment_id: str,
    *,
    revision: int = 1,
    expression_allowed: bool = True,
    internal_use_allowed: bool = True,
) -> Commitment:
    return Commitment(
        id=commitment_id,
        revision=revision,
        created_at=stack.clock.now_rfc3339(),
        subject_refs=(),
        custodian_refs=(),
        resource_id=f"resource:{commitment_id}",
        source_scope=None,
        strength=CommitmentStrength.STRONG,
        internal_use_allowed=internal_use_allowed,
        observable_influence_allowed=True,
        expression_allowed=expression_allowed,
        audience_predicate_id="predicate:public",
        policy_evidence_refs=(),
    )


def _act_plan(
    stack: ReviewStack,
    plan_id: str,
    *,
    snapshot,
    intent_ref: RecordRef,
    logical_effect_id: str | None = None,
    proposal_revision: int = 1,
    text: str = "a careful public note",
    target_ref: RecordRef | None = None,
):
    """A persisted ACT plan with a controllable logical effect id and
    proposal revision (speak_plan's shape; the supersession tests need two
    revisions of one logical id)."""
    now = stack.clock.now_rfc3339()
    if target_ref is None:
        rendered: dict = render_send_message(
            destination_id="room:public", text=text, reply_target_ref=None
        )
    else:
        # bind the digest from the TARGET FRAGMENT's own payload (the
        # coordinator's _target_content_digest rule), not a guessed id —
        # the purge-vs-dispatch regression depends on the binding being
        # live (review finding on xek).
        fragment = stack.core.get_record(target_ref.id, target_ref.revision)
        assert isinstance(fragment, ContentFragment)
        data = stack.core.get_payload(fragment.payload_ref)
        assert data is not None, "reaction target payload must be live to bind"
        rendered = render_add_reaction(
            destination_id="room:public",
            target_ref=target_ref,
            target_content_digest=hashlib.sha256(data.encode("utf-8")).hexdigest(),
            emoji="👋",
        )
    payload_id = f"payload:effect:{plan_id}"
    stack.core.put_payload(
        payload_id, canonical_json_bytes(rendered).decode("utf-8"), owner_ref=f"plan:{plan_id}"
    )
    effect = Effect(
        id=f"{plan_id}:effect:0",
        revision=1,
        created_at=now,
        logical_effect_id=logical_effect_id or f"{plan_id}:effect:0",
        kind=EffectKind.ADD_REACTION if target_ref is not None else EffectKind.SEND_MESSAGE,
        account_id=stack.capability.account_id,
        destination_id="room:public",
        target_ref=target_ref,
        rendered_payload_ref=payload_id,
        payload_digest=canonical_digest(rendered),
        capability_ref=RecordRef(id=stack.capability.id, revision=stack.capability.revision),
    )
    stack.core.put_record(effect)
    plan = EffectPlan(
        id=f"plan:{plan_id}",
        revision=1,
        created_at=now,
        intent_ref=intent_ref,
        snapshot_ref=RecordRef(id=snapshot.id, revision=snapshot.revision),
        disposition=Disposition.ACT,
        effects=(effect,),
        proposal_revision=proposal_revision,
        proposal_digest=canonical_digest(
            {
                "disposition": Disposition.ACT.value,
                "effects": [effect.payload_digest],
                "intent": intent_ref.to_payload(),
                "snapshot": snapshot.id,
                "coverage": snapshot.producer_coverage_ref.id,
            }
        ),
        defer_until=None,
        defer_expires_at=None,
        declared_fact_refs=(),
        observed_target_refs=(),
        producer_coverage_ref=RecordRef(
            id=snapshot.producer_coverage_ref.id, revision=snapshot.producer_coverage_ref.revision
        ),
    )
    stack.core.put_record(plan)
    return plan, rendered


def _authorize(stack: ReviewStack, plan: EffectPlan):
    decision = stack.gate.evaluate(plan, context=gate_context(stack))
    assert decision.kind == DecisionKind.AUTHORIZED, [c.value for c in decision.reason_codes]
    assert decision.authorization_ref is not None
    authorization = stack.core.get_record(
        decision.authorization_ref.id, decision.authorization_ref.revision
    )
    from agora.contracts import CommitAuthorization

    assert isinstance(authorization, CommitAuthorization)
    return authorization


def _submit(stack: ReviewStack, authorization, plan: EffectPlan, rendered: dict):
    return stack.sidecar.submit_authorized(
        authorization,
        plan.effects[0],
        rendered_payload=rendered,
        snapshot_ref=plan.snapshot_ref,
    )


def _world_rows(stack: ReviewStack) -> list[str]:
    """Delivered bytes only — the world's own visibility rule excludes
    recorded failure outcomes."""
    return [
        row["payload_bytes"]
        for row in stack.world._conn.execute(
            "SELECT payload_bytes FROM world_effects WHERE outcome != 'failure'"
            " ORDER BY visible_at_ms"
        ).fetchall()
    ]


def _reservations(stack: ReviewStack) -> list[dict]:
    return [
        dict(row)
        for row in stack.core._conn.execute(
            "SELECT * FROM core_release_reservations ORDER BY reservation_id"
        ).fetchall()
    ]


# -- 1.1 pre-dispatch revalidation ------------------------------------------


def test_none_route_authorization_binds_consulted_policy_vector(tmp_path: Path) -> None:
    """Routing result != routing evidence (D1): a NONE-route authorization
    binds the permissive commitment the walk consulted — reviewer never
    runs, but the evidence is on the authorization."""
    stack = ReviewStack(tmp_path)
    permissive = _commitment(stack, "commitment:permissive")
    stack.core.put_record(permissive)
    intent = make_intent(stack, "intent:none-vector")
    snapshot = review_snapshot(
        stack,
        "call:none-vector",
        intent,
        inherited_commitment_refs=(RecordRef(id=permissive.id, revision=1),),
    )
    plan, _rendered = _act_plan(stack, "none-vector", snapshot=snapshot, intent_ref=intent)
    authorization = _authorize(stack, plan)

    assert stack.reviewer.calls == 0  # route NONE
    vector = [(ref.id, ref.revision) for ref in authorization.policy_version_vector]
    assert ("commitment:permissive", 1) in vector  # the consulted evidence is bound


def test_policy_change_after_authorization_holds_before_dispatch(tmp_path: Path) -> None:
    """I08: a consulted commitment revised between authorization and
    dispatch holds the outbox row with POLICY_CHANGED — even though the
    route was NONE — and the world receives nothing."""
    stack = ReviewStack(tmp_path)
    permissive = _commitment(stack, "commitment:permissive")
    stack.core.put_record(permissive)
    intent = make_intent(stack, "intent:policy-hold")
    snapshot = review_snapshot(
        stack,
        "call:policy-hold",
        intent,
        inherited_commitment_refs=(RecordRef(id=permissive.id, revision=1),),
    )
    plan, rendered = _act_plan(stack, "policy-hold", snapshot=snapshot, intent_ref=intent)
    authorization = _authorize(stack, plan)

    # the commitment tightens after authorization, before dispatch
    stack.core.put_record(
        _commitment(stack, "commitment:permissive", revision=2, expression_allowed=False)
    )

    with pytest.raises(SidecarError) as excinfo:
        _submit(stack, authorization, plan, rendered)
    assert excinfo.value.code == ReasonCode.POLICY_CHANGED
    assert stack.transport.outbox_state(plan.effects[0].logical_effect_id) == "HELD"
    assert _world_rows(stack) == []


def test_edited_reaction_target_holds_before_dispatch(tmp_path: Path) -> None:
    """I08/TARGET_CHANGED: an edited target never receives a reaction to
    stale bytes."""
    from tests.fixtures_review import feed_event

    stack = ReviewStack(tmp_path)
    feed_event(stack)  # fragment:evt:1 with payload "hello hermes 1"
    intent = make_intent(stack, "intent:target-hold")
    snapshot = review_snapshot(stack, "call:target-hold", intent)
    plan, rendered = _act_plan(
        stack,
        "target-hold",
        snapshot=snapshot,
        intent_ref=intent,
        target_ref=RecordRef(id="fragment:evt:1", revision=1),
    )
    authorization = _authorize(stack, plan)

    stack.core.put_payload("payload:evt:1b", "hello hermes EDITED", owner_ref="evt:1")

    original = stack.core.get_record("fragment:evt:1")
    assert original is not None
    stack.core.put_record(
        replace(original, revision=2, payload_ref="payload:evt:1b", created_at=original.created_at)
    )

    with pytest.raises(SidecarError) as excinfo:
        _submit(stack, authorization, plan, rendered)
    assert excinfo.value.code == ReasonCode.TARGET_CHANGED
    assert stack.transport.outbox_state(plan.effects[0].logical_effect_id) == "HELD"
    assert _world_rows(stack) == []


def test_capability_revocation_holds_before_dispatch(tmp_path: Path) -> None:
    stack = ReviewStack(tmp_path)
    intent = make_intent(stack, "intent:cap-hold")
    snapshot = review_snapshot(stack, "call:cap-hold", intent)
    plan, rendered = _act_plan(stack, "cap-hold", snapshot=snapshot, intent_ref=intent)
    authorization = _authorize(stack, plan)

    stack.core.put_record(replace(stack.capability, revision=2, status=RecordStatus.REVOKED))

    with pytest.raises(SidecarError) as excinfo:
        _submit(stack, authorization, plan, rendered)
    assert excinfo.value.code == ReasonCode.CAPABILITY_INVALID
    assert stack.transport.outbox_state(plan.effects[0].logical_effect_id) == "HELD"
    assert _world_rows(stack) == []


def test_view_revocation_after_authorize_holds_before_dispatch(tmp_path: Path) -> None:
    """Bead l34 / I14: revoking a view the snapshot admitted invalidates
    that snapshot, so a queued effect derived through it holds with
    STALE_CONTEXT and nothing dispatches."""
    from agora.contracts import DependencyKind
    from tests.fixtures_review import comparison_view, feed_event, seed_five_classes

    stack = ReviewStack(tmp_path)
    seed_five_classes(stack)
    feed_event(stack)
    intent = make_intent(stack, "intent:view-hold")
    view = comparison_view(
        stack,
        "view:held",
        call_id="call:view-hold",
        fragment_ref=RecordRef(id="fragment:evt:1", revision=1),
        authorization_id="use:finance-fields",
    )
    snapshot = review_snapshot(stack, "call:view-hold", intent, views=(view,))
    stack.core.add_dependency(  # the edge production build_snapshot writes
        RecordRef(id=snapshot.id, revision=snapshot.revision),
        RecordRef(id=view.id, revision=view.revision),
        DependencyKind.PRODUCER_INPUT,
    )
    plan, rendered = _act_plan(stack, "view-hold", snapshot=snapshot, intent_ref=intent)
    authorization = _authorize(stack, plan)

    stack.context_memory.revoke_view(RecordRef(id=view.id, revision=1))

    with pytest.raises(SidecarError) as excinfo:
        _submit(stack, authorization, plan, rendered)
    assert excinfo.value.code == ReasonCode.STALE_CONTEXT
    assert stack.transport.outbox_state(plan.effects[0].logical_effect_id) == "HELD"
    assert _world_rows(stack) == []


def test_snapshot_invalidation_holds_before_dispatch(tmp_path: Path) -> None:
    """I14: a correction invalidating the snapshot after authorization
    holds the queued effect — dispatch nothing derived from stale context."""
    stack = ReviewStack(tmp_path)
    intent = make_intent(stack, "intent:snap-hold")
    snapshot = review_snapshot(stack, "call:snap-hold", intent)
    plan, rendered = _act_plan(stack, "snap-hold", snapshot=snapshot, intent_ref=intent)
    authorization = _authorize(stack, plan)

    stack.core.put_record(replace(snapshot, revision=2, status=SnapshotStatus.INVALID))

    with pytest.raises(SidecarError) as excinfo:
        _submit(stack, authorization, plan, rendered)
    assert excinfo.value.code == ReasonCode.STALE_CONTEXT
    assert stack.transport.outbox_state(plan.effects[0].logical_effect_id) == "HELD"
    assert _world_rows(stack) == []


def test_post_linearization_change_is_too_late_for_prevention(tmp_path: Path) -> None:
    """A change landing after DISPATCH_STARTED is not prevented: the
    in-flight delivery resolves conservatively through UNKNOWN and only
    positive world evidence settles it."""
    stack = ReviewStack(tmp_path)
    permissive = _commitment(stack, "commitment:permissive")
    stack.core.put_record(permissive)
    intent = make_intent(stack, "intent:late")
    snapshot = review_snapshot(
        stack,
        "call:late",
        intent,
        inherited_commitment_refs=(RecordRef(id=permissive.id, revision=1),),
    )
    plan, rendered = _act_plan(stack, "late", snapshot=snapshot, intent_ref=intent)
    authorization = _authorize(stack, plan)

    effect_id = plan.effects[0].logical_effect_id
    stack.world.script(f"attempt:{effect_id}:1", WorldOutcome("timeout"))
    receipt = _submit(stack, authorization, plan, rendered)
    assert receipt.state.value == "UNKNOWN"

    # the tightening lands after the linearization point
    stack.core.put_record(
        _commitment(stack, "commitment:permissive", revision=2, expression_allowed=False)
    )

    assert stack.sidecar.reconcile(effect_id) == ReconcileResult.APPLIED
    assert stack.transport.outbox_state(effect_id) == "SENT"  # the delivery stands


# -- 1.2 acknowledged supersession ------------------------------------------


def test_revision_requeue_requires_acknowledged_cancellation(tmp_path: Path) -> None:
    """I09: r1 held, r2 supersedes it only through acknowledged
    cancellation; exactly r2's bytes reach the world."""
    stack = ReviewStack(tmp_path)
    permissive = _commitment(stack, "commitment:permissive")
    stack.core.put_record(permissive)
    intent = make_intent(stack, "intent:sup")
    snapshot1 = review_snapshot(
        stack,
        "call:sup1",
        intent,
        inherited_commitment_refs=(RecordRef(id=permissive.id, revision=1),),
    )
    plan1, _r1 = _act_plan(
        stack,
        "sup:1",
        snapshot=snapshot1,
        intent_ref=intent,
        logical_effect_id="sup:logical",
        proposal_revision=1,
        text="first try",
    )
    auth1 = _authorize(stack, plan1)

    # r1 holds: the commitment gets a benign revision after authorization
    stack.core.put_record(
        _commitment(stack, "commitment:permissive", revision=2, internal_use_allowed=False)
    )
    with pytest.raises(SidecarError):
        _submit(stack, auth1, plan1, _r1)
    assert stack.transport.outbox_state("sup:logical") == "HELD"

    # r2 routes against the current commitment revision and requeues r1
    snapshot2 = review_snapshot(
        stack,
        "call:sup2",
        intent,
        inherited_commitment_refs=(RecordRef(id=permissive.id, revision=2),),
    )
    plan2, rendered2 = _act_plan(
        stack,
        "sup:2",
        snapshot=snapshot2,
        intent_ref=intent,
        logical_effect_id="sup:logical",
        proposal_revision=2,
        text="second and better",
    )
    auth2 = _authorize(stack, plan2)
    receipt = _submit(stack, auth2, plan2, rendered2)

    assert receipt.state.value == "SENT"
    rows = _world_rows(stack)
    assert len(rows) == 1 and "second and better" in rows[0]


def test_too_late_refuses_requeue_and_earlier_receipt_stands(tmp_path: Path) -> None:
    stack = ReviewStack(tmp_path)
    intent = make_intent(stack, "intent:toolate")
    snapshot = review_snapshot(stack, "call:toolate", intent)
    plan1, rendered1 = _act_plan(
        stack,
        "toolate:1",
        snapshot=snapshot,
        intent_ref=intent,
        logical_effect_id="toolate:logical",
        proposal_revision=1,
        text="the original",
    )
    auth1 = _authorize(stack, plan1)
    first_receipt = _submit(stack, auth1, plan1, rendered1)
    assert first_receipt.state.value == "SENT"

    plan2, rendered2 = _act_plan(
        stack,
        "toolate:2",
        snapshot=snapshot,
        intent_ref=intent,
        logical_effect_id="toolate:logical",
        proposal_revision=2,
        text="the replacement",
    )
    auth2 = _authorize(stack, plan2)
    with pytest.raises(SidecarError) as excinfo:
        _submit(stack, auth2, plan2, rendered2)
    assert excinfo.value.code == ReasonCode.TOO_LATE
    assert stack.transport.outbox_state("toolate:logical") == "SENT"
    rows = _world_rows(stack)
    assert len(rows) == 1 and "the original" in rows[0]


def test_cancel_ack_survives_crash_before_requeue(tmp_path: Path) -> None:
    """The cancellation acknowledgement is durable: a requeue submitted
    after the cancel completes is exactly the designed crash-recovery
    shape."""
    stack = ReviewStack(tmp_path)
    intent = make_intent(stack, "intent:crashsup")
    snapshot = review_snapshot(stack, "call:crashsup", intent)
    plan1, rendered1 = _act_plan(
        stack,
        "crashsup:1",
        snapshot=snapshot,
        intent_ref=intent,
        logical_effect_id="crashsup:logical",
        proposal_revision=1,
        text="hold me",
    )
    auth1 = _authorize(stack, plan1)
    stack.core.put_record(replace(stack.capability, revision=2, status=RecordStatus.REVOKED))
    with pytest.raises(SidecarError):
        _submit(stack, auth1, plan1, rendered1)
    stack.core.put_record(replace(stack.capability, revision=3, status=RecordStatus.ACTIVE))
    restored = stack.core.get_record("capability:social")
    assert isinstance(restored, CapabilityEnvelope)
    stack.capability = restored

    assert stack.sidecar.cancel_or_supersede("crashsup:logical") == "CANCELLED"

    plan2, rendered2 = _act_plan(
        stack,
        "crashsup:2",
        snapshot=snapshot,
        intent_ref=intent,
        logical_effect_id="crashsup:logical",
        proposal_revision=2,
        text="after the crash",
    )
    auth2 = _authorize(stack, plan2)
    receipt = _submit(stack, auth2, plan2, rendered2)
    assert receipt.state.value == "SENT"
    rows = _world_rows(stack)
    assert len(rows) == 1 and "after the crash" in rows[0]


def test_cancelled_revision_cannot_be_resubmitted(tmp_path: Path) -> None:
    stack = ReviewStack(tmp_path)
    intent = make_intent(stack, "intent:cancelresub")
    snapshot = review_snapshot(stack, "call:cancelresub", intent)
    plan1, rendered1 = _act_plan(
        stack,
        "cancelresub:1",
        snapshot=snapshot,
        intent_ref=intent,
        logical_effect_id="cancelresub:logical",
        proposal_revision=1,
    )
    auth1 = _authorize(stack, plan1)
    stack.core.put_record(replace(stack.audience, revision=2, disclosure_context_revision=2))
    with pytest.raises(SidecarError):
        _submit(stack, auth1, plan1, rendered1)  # HELD, undispatched
    assert stack.sidecar.cancel_or_supersede("cancelresub:logical") == "CANCELLED"
    with pytest.raises(SidecarError) as excinfo:
        _submit(stack, auth1, plan1, rendered1)
    assert excinfo.value.code == ReasonCode.CONFLICT


def test_exact_duplicate_submit_returns_current_state(tmp_path: Path) -> None:
    stack = ReviewStack(tmp_path)
    intent = make_intent(stack, "intent:dup")
    snapshot = review_snapshot(stack, "call:dup", intent)
    plan, rendered = _act_plan(stack, "dup", snapshot=snapshot, intent_ref=intent)
    authorization = _authorize(stack, plan)
    first = _submit(stack, authorization, plan, rendered)
    second = _submit(stack, authorization, plan, rendered)
    assert second.id == first.id and second.state == first.state
    assert len(_world_rows(stack)) == 1


# -- 1.3 material audience change -------------------------------------------


def test_material_audience_change_holds_while_chatter_dispatches(tmp_path: Path) -> None:
    """D3: a disclosure-revision move (or visibility flip) is material;
    ordinary new chatter in the same audience shape is not."""
    stack = ReviewStack(tmp_path)
    intent = make_intent(stack, "intent:aud")
    snapshot = review_snapshot(stack, "call:aud1", intent)
    plan1, rendered1 = _act_plan(stack, "aud:1", snapshot=snapshot, intent_ref=intent)
    auth1 = _authorize(stack, plan1)

    # the audience's disclosure context moves after authorization
    stack.core.put_record(replace(stack.audience, revision=2, disclosure_context_revision=2))
    with pytest.raises(SidecarError) as excinfo:
        _submit(stack, auth1, plan1, rendered1)
    assert excinfo.value.code == ReasonCode.AUDIENCE_CHANGED
    assert stack.transport.outbox_state(plan1.effects[0].logical_effect_id) == "HELD"
    assert _world_rows(stack) == []

    # a fresh effect under the current (stable) audience shape dispatches:
    # ordinary chatter — message growth, same visibility — is not material
    refreshed_audience = stack.core.get_record("audience:room:public")
    assert isinstance(refreshed_audience, AudienceSnapshot)
    stack.audience = refreshed_audience
    snapshot2 = review_snapshot(stack, "call:aud2", intent)
    plan2, rendered2 = _act_plan(stack, "aud:2", snapshot=snapshot2, intent_ref=intent)
    auth2 = _authorize(stack, plan2)
    receipt = _submit(stack, auth2, plan2, rendered2)
    assert receipt.state.value == "SENT"
    assert len(_world_rows(stack)) == 1


def test_unknown_readers_flip_is_material(tmp_path: Path) -> None:
    stack = ReviewStack(tmp_path)
    intent = make_intent(stack, "intent:anon")
    snapshot = review_snapshot(stack, "call:anon", intent)
    plan, rendered = _act_plan(stack, "anon", snapshot=snapshot, intent_ref=intent)
    authorization = _authorize(stack, plan)

    stack.core.put_record(replace(stack.audience, revision=2, unknown_readers=False))
    with pytest.raises(SidecarError) as excinfo:
        _submit(stack, authorization, plan, rendered)
    assert excinfo.value.code == ReasonCode.AUDIENCE_CHANGED


# -- 1.4 release limits ------------------------------------------------------


def _seed_rate_limit(stack: ReviewStack, limit: int) -> None:
    refreshed = replace(
        stack.capability, revision=2, rate_budget_limits=(Pair(first="per_minute", second=limit),)
    )
    stack.core.put_record(refreshed)
    assert isinstance(refreshed, CapabilityEnvelope)
    stack.capability = refreshed


def test_release_limits_admit_several_per_window_and_rollover(tmp_path: Path) -> None:
    """I15/D4: per_minute = 2 admits two one-unit releases in a window,
    holds the third with RELEASE_LIMIT, and a new window admits it — a
    limit above one is a real budget, not a one-bit mutex."""
    stack = ReviewStack(tmp_path)
    _seed_rate_limit(stack, 2)
    intent = make_intent(stack, "intent:rate")

    receipts = []
    for index in range(3):
        snapshot = review_snapshot(stack, f"call:rate{index}", intent)
        plan, rendered = _act_plan(stack, f"rate:{index}", snapshot=snapshot, intent_ref=intent)
        authorization = _authorize(stack, plan)
        if index < 2:
            receipts.append(_submit(stack, authorization, plan, rendered))
        else:
            with pytest.raises(SidecarError) as excinfo:
                _submit(stack, authorization, plan, rendered)
            assert excinfo.value.code == ReasonCode.RELEASE_LIMIT
            assert stack.transport.outbox_state(plan.effects[0].logical_effect_id) == "HELD"
            held_plan, held_rendered, held_auth = plan, rendered, authorization

    assert all(receipt.state.value == "SENT" for receipt in receipts)
    assert len(_world_rows(stack)) == 2
    assert len(_reservations(stack)) == 2

    # the virtual clock crosses into the next window; the held effect
    # re-enters through the queue and reserves fresh
    stack.clock.advance_ms(60_000)
    receipt = _submit(stack, held_auth, held_plan, held_rendered)
    assert receipt.state.value == "SENT"
    rows = _reservations(stack)
    assert len(rows) == 3
    keys = {row["query_key"] for row in rows}
    assert len(keys) == 2  # two distinct windows
    assert all(row["state"] == "COMMITTED" for row in rows)
    assert len(_world_rows(stack)) == 3


def test_release_limit_of_one_is_exhausted_by_a_single_release(tmp_path: Path) -> None:
    stack = ReviewStack(tmp_path)
    _seed_rate_limit(stack, 1)
    intent = make_intent(stack, "intent:rate1")

    snapshot = review_snapshot(stack, "call:rate1a", intent)
    plan_a, rendered_a = _act_plan(stack, "rate1:a", snapshot=snapshot, intent_ref=intent)
    receipt = _submit(stack, _authorize(stack, plan_a), plan_a, rendered_a)
    assert receipt.state.value == "SENT"

    plan_b, rendered_b = _act_plan(stack, "rate1:b", snapshot=snapshot, intent_ref=intent)
    with pytest.raises(SidecarError) as excinfo:
        _submit(stack, _authorize(stack, plan_b), plan_b, rendered_b)
    assert excinfo.value.code == ReasonCode.RELEASE_LIMIT
    assert stack.transport.outbox_state(plan_b.effects[0].logical_effect_id) == "HELD"
    assert len(_world_rows(stack)) == 1


# -- 1.5 retry and reconciliation -------------------------------------------


def test_failed_confirmed_retries_once_under_fresh_attempt(tmp_path: Path) -> None:
    """I11: FAILED_CONFIRMED with certified no-effect evidence retries
    exactly once under a fresh attempt id (logical id preserved); a third
    attempt does not exist."""
    stack = ReviewStack(tmp_path)
    intent = make_intent(stack, "intent:retry")
    snapshot = review_snapshot(stack, "call:retry", intent)
    plan, rendered = _act_plan(stack, "retry", snapshot=snapshot, intent_ref=intent)
    authorization = _authorize(stack, plan)
    effect_id = plan.effects[0].logical_effect_id

    from agora.fake_sidecar import WorldOutcome

    stack.world.script(f"attempt:{effect_id}:1", WorldOutcome("failure"))
    first = _submit(stack, authorization, plan, rendered)
    assert first.state.value == "FAILED_CONFIRMED"

    stack.world.script(f"attempt:{effect_id}:2", WorldOutcome("success"))
    second = stack.sidecar.retry_failed(
        authorization, plan.effects[0], rendered_payload=rendered, snapshot_ref=plan.snapshot_ref
    )
    assert second.state.value == "SENT"
    assert second.attempt_id != first.attempt_id  # fresh attempt, same logical id
    assert second.effect_id == effect_id

    # a SENT effect has nothing to retry: typed misuse, and no third
    # attempt is ever created
    with pytest.raises(SidecarError) as excinfo:
        stack.sidecar.retry_failed(
            authorization,
            plan.effects[0],
            rendered_payload=rendered,
            snapshot_ref=plan.snapshot_ref,
        )
    assert excinfo.value.code == ReasonCode.CONFLICT
    attempts = stack.transport._conn.execute(
        "SELECT count(*) FROM transport_attempts WHERE effect_id = ?", (effect_id,)
    ).fetchone()[0]
    assert attempts == 2  # no third attempt exists
    assert len(_world_rows(stack)) == 1  # only the retry delivered


def test_retry_budget_is_exhausted_after_one_retry(tmp_path: Path) -> None:
    """I11: the retry is exactly once — a twice-failed effect refuses a
    third attempt with TOO_LATE."""
    stack = ReviewStack(tmp_path)
    intent = make_intent(stack, "intent:retry2")
    snapshot = review_snapshot(stack, "call:retry2", intent)
    plan, rendered = _act_plan(stack, "retry2", snapshot=snapshot, intent_ref=intent)
    authorization = _authorize(stack, plan)
    effect_id = plan.effects[0].logical_effect_id

    stack.world.script(f"attempt:{effect_id}:1", WorldOutcome("failure"))
    stack.world.script(f"attempt:{effect_id}:2", WorldOutcome("failure"))
    first = _submit(stack, authorization, plan, rendered)
    assert first.state.value == "FAILED_CONFIRMED"
    second = stack.sidecar.retry_failed(
        authorization, plan.effects[0], rendered_payload=rendered, snapshot_ref=plan.snapshot_ref
    )
    assert second.state.value == "FAILED_CONFIRMED"
    with pytest.raises(SidecarError) as excinfo:
        stack.sidecar.retry_failed(
            authorization,
            plan.effects[0],
            rendered_payload=rendered,
            snapshot_ref=plan.snapshot_ref,
        )
    assert excinfo.value.code == ReasonCode.TOO_LATE
    attempts = stack.transport._conn.execute(
        "SELECT count(*) FROM transport_attempts WHERE effect_id = ?", (effect_id,)
    ).fetchone()[0]
    assert attempts == 2


def test_unknown_resolves_only_on_positive_world_evidence(tmp_path: Path) -> None:
    """I11/§6.2: a timeout is UNKNOWN; APPLIED resolves it to SENT, and
    INDETERMINATE (a crash before the world call, DISPATCH_STARTED at
    restart) leaves it UNKNOWN — never a blind re-send."""
    stack = ReviewStack(tmp_path)
    intent = make_intent(stack, "intent:unk")
    snapshot = review_snapshot(stack, "call:unk", intent)
    plan, rendered = _act_plan(stack, "unk", snapshot=snapshot, intent_ref=intent)
    authorization = _authorize(stack, plan)
    effect_id = plan.effects[0].logical_effect_id

    from agora.fake_sidecar import ReconcileResult, WorldOutcome

    stack.world.script(f"attempt:{effect_id}:1", WorldOutcome("timeout"))
    receipt = _submit(stack, authorization, plan, rendered)
    assert receipt.state.value == "UNKNOWN"
    assert stack.sidecar.reconcile(effect_id) == ReconcileResult.APPLIED
    assert stack.transport.outbox_state(effect_id) == "SENT"

    # a crash after DISPATCH_STARTED but before the world call: with no
    # world evidence, recovery marks the in-flight effect UNKNOWN — never
    # guesses no-send, never re-sends
    stack.injector.arm(CrashCheckpoint.AFTER_DISPATCH_START_BEFORE_WORLD)
    intent2 = make_intent(stack, "intent:unk2")
    snapshot2 = review_snapshot(stack, "call:unk2", intent2)
    plan2, rendered2 = _act_plan(stack, "unk2", snapshot=snapshot2, intent_ref=intent2)
    auth2 = _authorize(stack, plan2)
    effect2 = plan2.effects[0].logical_effect_id
    with pytest.raises(InjectedCrash):
        _submit(stack, auth2, plan2, rendered2)
    assert stack.transport.outbox_state(effect2) == "DISPATCH_STARTED"
    stack.injector.disarm(CrashCheckpoint.AFTER_DISPATCH_START_BEFORE_WORLD)
    assert stack.sidecar.reconcile(effect2) == ReconcileResult.INDETERMINATE
    assert stack.transport.outbox_state(effect2) == "UNKNOWN"


def test_cancellation_is_idempotent_across_during_cancel(tmp_path: Path) -> None:
    stack = ReviewStack(tmp_path)
    intent = make_intent(stack, "intent:idemcancel")
    snapshot = review_snapshot(stack, "call:idemcancel", intent)
    plan, rendered = _act_plan(stack, "idemcancel", snapshot=snapshot, intent_ref=intent)
    authorization = _authorize(stack, plan)
    effect_id = plan.effects[0].logical_effect_id

    # leave the effect queued-but-undispatched (crash past the consume
    # window, before the linearization point)
    stack.injector.arm(CrashCheckpoint.AFTER_LEASE_CONSUME_BEFORE_DISPATCH_START)
    with pytest.raises(InjectedCrash):
        _submit(stack, authorization, plan, rendered)
    stack.injector.disarm(CrashCheckpoint.AFTER_LEASE_CONSUME_BEFORE_DISPATCH_START)
    assert stack.transport.outbox_state(effect_id) == "QUEUED"

    stack.injector.arm(CrashCheckpoint.DURING_CANCEL)
    with pytest.raises(InjectedCrash):
        stack.sidecar.cancel_or_supersede(effect_id)
    stack.injector.disarm(CrashCheckpoint.DURING_CANCEL)

    assert stack.sidecar.cancel_or_supersede(effect_id) == "CANCELLED"
    assert stack.sidecar.cancel_or_supersede(effect_id) == "CANCELLED"  # idempotent
    assert stack.transport.outbox_state(effect_id) == "CANCELLED"


# -- reservation settlement ---------------------------------------------------


def test_cancelled_effect_releases_its_unused_reservation(tmp_path: Path) -> None:
    """The addendum's state machine: acknowledged pre-dispatch cancellation
    is the only refund; a superseded revision spends fresh in its own
    window."""
    stack = ReviewStack(tmp_path)
    _seed_rate_limit(stack, 1)
    intent = make_intent(stack, "intent:refund")
    snapshot1 = review_snapshot(stack, "call:refund1", intent)
    plan1, rendered1 = _act_plan(
        stack,
        "refund:1",
        snapshot=snapshot1,
        intent_ref=intent,
        logical_effect_id="refund:logical",
        proposal_revision=1,
    )
    auth1 = _authorize(stack, plan1)
    # crash past the consume window: the reservation is spent but nothing
    # dispatched, exactly the state an unused allowance can be released from
    stack.injector.arm(CrashCheckpoint.AFTER_LEASE_CONSUME_BEFORE_DISPATCH_START)
    with pytest.raises(InjectedCrash):
        _submit(stack, auth1, plan1, rendered1)
    stack.injector.disarm(CrashCheckpoint.AFTER_LEASE_CONSUME_BEFORE_DISPATCH_START)
    rows = _reservations(stack)
    assert len(rows) == 1 and rows[0]["state"] == "RESERVED"

    assert stack.sidecar.cancel_or_supersede("refund:logical") == "CANCELLED"
    rows = _reservations(stack)
    assert rows[0]["state"] == "CANCELLED_BEFORE_DISPATCH"

    # r2 dispatches in the same window on the released allowance
    snapshot2 = review_snapshot(stack, "call:refund2", intent)
    plan2, rendered2 = _act_plan(
        stack,
        "refund:2",
        snapshot=snapshot2,
        intent_ref=intent,
        logical_effect_id="refund:logical",
        proposal_revision=2,
    )
    auth2 = _authorize(stack, plan2)
    receipt = _submit(stack, auth2, plan2, rendered2)
    assert receipt.state.value == "SENT"
    rows = _reservations(stack)
    assert len(rows) == 1 and rows[0]["state"] == "COMMITTED"  # reactivated, not duplicated


def test_rate_limited_turn_surfaces_a_typed_hold(tmp_path: Path) -> None:
    """Coordinator integration: a dispatch-time hold reaches the turn
    outcome as a typed HELD decision (the bounded rewrite loop's notice
    path), with nothing dispatched over budget."""
    from tests.fixtures_review import feed_event, speak_step

    stack = ReviewStack(tmp_path, [speak_step("first within budget"), speak_step("second over")])
    _seed_rate_limit(stack, 1)
    assert isinstance(stack.coordinator.capability, CapabilityEnvelope)
    stack.coordinator.capability = stack.capability  # bind the limited revision

    feed_event(stack, 1)
    feed_event(stack, 2)
    from agora.contracts import OutcomeKind

    first = stack.coordinator.run_turn(stack.coordinator.ready_work_ids()[0])
    assert first.outcome.kind == OutcomeKind.COMMITTED
    second = stack.coordinator.run_turn(stack.coordinator.ready_work_ids()[0])
    assert second.outcome.kind == OutcomeKind.HELD
    assert "RELEASE_LIMIT" in [code.value for code in second.outcome.reason_codes]
    assert len(_world_rows(stack)) == 1  # nothing dispatched over budget


def test_cancelling_a_failed_revision_keeps_its_allowance_spent(tmp_path: Path) -> None:
    """Review finding on D4/I15: a FAILED_CONFIRMED revision was
    dispatched, so superseding it never refunds or mislabels its window
    slot. The accounting unit is per logical effect (one reservation row
    per effect/constraint): the replacement revision rides the same spent
    slot in the same window, the slot is never released to other effects,
    and the ledger never claims CANCELLED_BEFORE_DISPATCH for an attempt
    that did start dispatch."""
    stack = ReviewStack(tmp_path)
    _seed_rate_limit(stack, 1)
    intent = make_intent(stack, "intent:spent")
    snapshot1 = review_snapshot(stack, "call:spent1", intent)
    plan1, rendered1 = _act_plan(
        stack,
        "spent:1",
        snapshot=snapshot1,
        intent_ref=intent,
        logical_effect_id="spent:logical",
        proposal_revision=1,
    )
    auth1 = _authorize(stack, plan1)
    stack.world.script("attempt:spent:logical:1", WorldOutcome("failure"))
    first = _submit(stack, auth1, plan1, rendered1)
    assert first.state.value == "FAILED_CONFIRMED"
    assert _reservations(stack)[0]["state"] == "RESERVED"  # spent, not refunded

    # cancelling the failed revision acknowledges it but never settles the
    # row to CANCELLED_BEFORE_DISPATCH — the attempt did start dispatch
    assert stack.sidecar.cancel_or_supersede("spent:logical") == "CANCELLED"
    assert _reservations(stack)[0]["state"] == "RESERVED"

    # the replacement rides the shared per-effect slot in the SAME window
    snapshot2 = review_snapshot(stack, "call:spent2", intent)
    plan2, rendered2 = _act_plan(
        stack,
        "spent:2",
        snapshot=snapshot2,
        intent_ref=intent,
        logical_effect_id="spent:logical",
        proposal_revision=2,
    )
    auth2 = _authorize(stack, plan2)
    receipt = _submit(stack, auth2, plan2, rendered2)
    assert receipt.state.value == "SENT"
    rows = _reservations(stack)
    assert len(rows) == 1 and rows[0]["state"] == "COMMITTED"  # one slot, one effect

    # and the occupied slot held a DIFFERENT effect out of the same window
    snapshot3 = review_snapshot(stack, "call:spent3", intent)
    plan3, rendered3 = _act_plan(stack, "spent:3", snapshot=snapshot3, intent_ref=intent)
    auth3 = _authorize(stack, plan3)
    with pytest.raises(SidecarError) as excinfo:
        _submit(stack, auth3, plan3, rendered3)
    assert excinfo.value.code == ReasonCode.RELEASE_LIMIT


def test_conflicting_bytes_for_same_revision_is_a_typed_conflict(tmp_path: Path) -> None:
    """Review finding on D2: the same logical id + proposal revision with
    different bytes refuses as the spec's typed CONFLICT — authorization
    ids key by logical id and revision, so the gate itself refuses a
    re-authorization of different bytes, and the sidecar's digest binding
    refuses the direct-API route; either way nothing re-sends."""
    from agora.stores import RecordConflictError

    stack = ReviewStack(tmp_path)
    intent = make_intent(stack, "intent:bytes")
    snapshot = review_snapshot(stack, "call:bytes", intent)
    plan, rendered = _act_plan(
        stack,
        "bytes",
        snapshot=snapshot,
        intent_ref=intent,
        logical_effect_id="bytes:logical",
        proposal_revision=2,
        text="original bytes",
    )
    authorization = _authorize(stack, plan)
    _submit(stack, authorization, plan, rendered)

    conflicting, _conflicting_rendered = _act_plan(
        stack,
        "bytes:x",
        snapshot=snapshot,
        intent_ref=intent,
        logical_effect_id="bytes:logical",
        proposal_revision=2,
        text="different bytes",
    )
    with pytest.raises(RecordConflictError):
        _authorize(stack, conflicting)  # the gate refuses the re-authorization

    tampered = dict(rendered)
    tampered["text"] = "different bytes"
    with pytest.raises(SidecarError) as excinfo:
        stack.sidecar.submit_authorized(
            authorization,
            plan.effects[0],
            rendered_payload=tampered,
            snapshot_ref=plan.snapshot_ref,
        )
    assert excinfo.value.code == ReasonCode.CONFLICT  # digest binding refuses
    assert len(_world_rows(stack)) == 1


def test_purged_target_holds_at_dispatch_revalidation(tmp_path: Path) -> None:
    """Bead xek-11: purge zeroes target bytes at the SAME revision, which
    the revision-only TARGET_CHANGED check cannot see — revalidation now
    also verifies the target still hashes to the content digest bound in
    the rendered bytes, so a purged target holds with TARGET_CHANGED and
    nothing sends."""
    from tests.fixtures_review import feed_event

    stack = ReviewStack(tmp_path)
    feed_event(stack)
    intent = make_intent(stack, "intent:purge-hold")
    snapshot = review_snapshot(stack, "call:purge-hold", intent)
    target = RecordRef(id="fragment:evt:1", revision=1)
    plan, rendered = _act_plan(
        stack, "purge-hold", snapshot=snapshot, intent_ref=intent, target_ref=target
    )
    authorization = _authorize(stack, plan)

    stack.core._conn.execute(
        "UPDATE core_payloads SET status = 'PURGED' WHERE payload_id = 'payload:evt:1'"
    )

    with pytest.raises(SidecarError) as excinfo:
        _submit(stack, authorization, plan, rendered)
    assert excinfo.value.code == ReasonCode.TARGET_CHANGED
    assert stack.transport.outbox_state(plan.effects[0].logical_effect_id) == "HELD"
    assert _world_rows(stack) == []


def test_gate_enforces_message_length_on_rendered_bytes(tmp_path: Path) -> None:
    """Bead xek-12: the hard length limit binds at the gate too (the
    effect monopoly), on the rendered bytes — a payload whose digest is
    intact cannot smuggle length past draft rendering."""
    from tests.fixtures_review import gate_context

    stack = ReviewStack(tmp_path)
    intent = make_intent(stack, "intent:length")
    snapshot = review_snapshot(stack, "call:length", intent)
    plan, _rendered = _act_plan(
        stack, "length", snapshot=snapshot, intent_ref=intent, text="x" * 2500
    )
    decision = stack.gate.evaluate(plan, context=gate_context(stack))
    assert decision.kind == DecisionKind.DENIED
    assert ReasonCode.POLICY_FORBIDDEN in decision.reason_codes
