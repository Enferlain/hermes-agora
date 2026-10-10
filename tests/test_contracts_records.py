"""Contract-layer tests: headers, validation, canonical binding, runtime-only
discipline, and the model-step boundary (handoff §10 order item 1)."""

from __future__ import annotations

import pytest

from agora.contracts import (
    ALL_RECORD_TYPES,
    AccessStatus,
    CanonicalizationError,
    DeliveryReceipt,
    DeliveryState,
    Disposition,
    Effect,
    EffectKind,
    EffectPlan,
    InboundEvent,
    IntendedUse,
    MainStepKind,
    MemoryAccessRequest,
    MemoryAccessResult,
    ModelRequestAccessStep,
    ModelSubmitPlanStep,
    Principal,
    PrincipalKind,
    ReasonCode,
    RecordRef,
    RequestedPrecision,
    ValidationError,
    ViewKind,
    canonical_digest,
    canonical_json_bytes,
    check_version_vector,
    parse_model_step,
    record_from_payload,
    record_to_payload,
    render_add_reaction,
    render_send_message,
)

UTC = "2026-10-10T00:00:00Z"
REF = RecordRef(id="principal:agent", revision=1)


def make_principal(revision: int = 1) -> Principal:
    return Principal(
        id="principal:agent",
        revision=revision,
        created_at=UTC,
        kind=PrincipalKind.AGENT,
        verified_bindings=(REF,),
        claimed_aliases=("hermes",),
    )


# -- header -----------------------------------------------------------------


def test_header_rejects_wrong_schema_version() -> None:
    with pytest.raises(ValidationError):
        Principal(
            schema_version=3,
            id="principal:agent",
            revision=1,
            created_at=UTC,
            kind=PrincipalKind.AGENT,
            verified_bindings=(),
            claimed_aliases=(),
        )


def test_header_rejects_bad_id_revision_and_timestamp() -> None:
    def build(record_id: str = "principal:x", revision: int = 1, created: str = UTC) -> Principal:
        return Principal(
            id=record_id,
            revision=revision,
            created_at=created,
            kind=PrincipalKind.AGENT,
            verified_bindings=(),
            claimed_aliases=(),
        )

    with pytest.raises(ValidationError):
        build(record_id="bad id!")
    with pytest.raises(ValidationError):
        build(revision=0)
    with pytest.raises(ValidationError):
        build(created="2026-10-10 00:00:00")
    with pytest.raises(ValidationError):
        build(revision=True)  # type: ignore[arg-type]  (bool is an int subtype; runtime rejects)


# -- strict field sets --------------------------------------------------------


def test_unknown_field_rejected_on_decode() -> None:
    payload = record_to_payload(make_principal())
    payload["unexpected"] = 1
    with pytest.raises(ValidationError):
        record_from_payload(Principal, payload)


def test_missing_field_rejected_on_decode() -> None:
    payload = record_to_payload(make_principal())
    del payload["kind"]
    with pytest.raises(ValidationError):
        record_from_payload(Principal, payload)


def test_unknown_enum_rejected() -> None:
    payload = record_to_payload(make_principal())
    payload["kind"] = "SUPERUSER"
    with pytest.raises(ValidationError):
        record_from_payload(Principal, payload)


def test_unknown_event_kind_rejected() -> None:
    event = {
        "id": "event:1",
        "revision": 1,
        "created_at": UTC,
        "source_event_id": "evt:1",
        "journal_seq": 1,
        "kind": "MEMBER_JOINED",
        "actor_ref": {"id": "principal:agent", "revision": 1},
        "occurred_at": UTC,
        "ingested_at": UTC,
        "source_audience_ref": {"id": "audience:room", "revision": 1},
        "fragment_refs": [],
        "reply_target_refs": [],
        "target_refs": [],
        "observed_trigger_refs": [],
        "recovery": "COMPLETE",
    }
    with pytest.raises(ValidationError):
        record_from_payload(InboundEvent, event)


# -- refs and version vectors --------------------------------------------------


def test_record_ref_requires_positive_revision() -> None:
    with pytest.raises(ValidationError):
        RecordRef(id="principal:agent", revision=0)


def test_version_vector_rejects_duplicate_ids_and_sorts() -> None:
    a = RecordRef(id="a:1", revision=1)
    b = RecordRef(id="b:1", revision=2)
    with pytest.raises(ValidationError):
        check_version_vector((a, RecordRef(id="a:1", revision=3)), "vv")
    assert check_version_vector((b, a), "vv") == (a, b)


# -- canonical binding ----------------------------------------------------------


def test_canonical_binding_sorts_unordered_labelled_pairs() -> None:
    first = {"limits": [{"first": "b", "second": 1}, {"first": "a", "second": 2}]}
    second = {"limits": [{"first": "a", "second": 2}, {"first": "b", "second": 1}]}
    # The record layer sorts label-keyed pairs before serialization; raw dicts
    # with different key ORDER also bind identically:
    assert canonical_json_bytes({"b": 1, "a": 2}) == canonical_json_bytes({"a": 2, "b": 1})
    assert canonical_digest(first) != canonical_digest(second)  # unsorted stays unsorted


def test_canonical_binding_preserves_ordered_content() -> None:
    ordered_a = {"actions": ["first", "second", "third"]}
    ordered_b = {"actions": ["third", "second", "first"]}
    assert canonical_json_bytes(ordered_a) != canonical_json_bytes(ordered_b)


def test_canonical_binding_rejects_nonfinite_numbers() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes({"x": float("nan")})
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes({"x": float("inf")})


def test_canonical_binding_rejects_unpaired_surrogates() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes({"text": "\ud800"})


def test_canonical_binding_is_tight_and_sorted() -> None:
    raw = canonical_json_bytes({"z": 1, "a": [1, 2]})
    assert raw == b'{"a":[1,2],"z":1}'


def test_rendered_payload_shapes() -> None:
    message = render_send_message(destination_id="room:public", text="hi", reply_target_ref=None)
    assert message == {
        "kind": "SEND_MESSAGE",
        "destination_id": "room:public",
        "text": "hi",
        "reply_target_ref": None,
    }
    target = RecordRef(id="msg:1", revision=1)
    reaction = render_add_reaction(
        destination_id="room:public",
        target_ref=target,
        target_content_digest="0" * 64,
        emoji="🙂",
    )
    assert reaction["target_ref"] == {"id": "msg:1", "revision": 1}
    assert '"emoji":"🙂"' in canonical_json_bytes(reaction).decode("utf-8")


# -- reason-code registry closure ------------------------------------------------


def test_reason_code_registry_is_closed() -> None:
    assert len({code.value for code in ReasonCode}) == 33
    with pytest.raises(ValidationError):
        record_from_payload(
            DeliveryReceipt,
            {
                "id": "receipt:1",
                "revision": 1,
                "created_at": UTC,
                "effect_id": "effect:1",
                "proposal_revision": 1,
                "attempt_id": "attempt:1",
                "state": DeliveryState.SENT.value,
                "fake_remote_id": None,
                "generation": 1,
                "error_code": "NOT_A_CODE",
                "observed_at": UTC,
            },
        )


# -- cross-field record rules ------------------------------------------------


def test_memory_access_result_granted_requires_view() -> None:
    def build(view_kind: ViewKind, view_ref: RecordRef | None) -> MemoryAccessResult:
        return MemoryAccessResult(
            id="result:1",
            revision=1,
            created_at=UTC,
            request_id="request:1",
            access_status=AccessStatus.GRANTED,
            view_kind=view_kind,
            view_ref=view_ref,
            reason_codes=(),
            safe_feedback_template_id=None,
            permitted_uses=(),
            producer_coverage_ref=None,
            transform_certificate_ref=None,
            authorization_refs=(),
            inherited_commitment_refs=(),
        )

    with pytest.raises(ValidationError):
        build(view_kind=ViewKind.NONE, view_ref=None)
    with pytest.raises(ValidationError):
        build(view_kind=ViewKind.FULL, view_ref=None)


def test_memory_access_result_denied_has_no_view() -> None:
    with pytest.raises(ValidationError):
        MemoryAccessResult(
            id="result:2",
            revision=1,
            created_at=UTC,
            request_id="request:1",
            access_status=AccessStatus.DENIED,
            view_kind=ViewKind.FULL,
            view_ref=REF,
            reason_codes=(),
            safe_feedback_template_id=None,
            permitted_uses=(),
            producer_coverage_ref=None,
            transform_certificate_ref=None,
            authorization_refs=(),
            inherited_commitment_refs=(),
        )


def test_memory_access_result_denied_carries_no_permissions() -> None:
    def build(
        permitted_uses: tuple[IntendedUse, ...] = (),
        transform_certificate_ref: RecordRef | None = None,
    ) -> MemoryAccessResult:
        return MemoryAccessResult(
            id="result:3",
            revision=1,
            created_at=UTC,
            request_id="request:1",
            access_status=AccessStatus.DENIED,
            view_kind=ViewKind.NONE,
            view_ref=None,
            reason_codes=(),
            safe_feedback_template_id=None,
            permitted_uses=permitted_uses,
            producer_coverage_ref=None,
            transform_certificate_ref=transform_certificate_ref,
            authorization_refs=(),
            inherited_commitment_refs=(),
        )

    with pytest.raises(ValidationError):
        build(permitted_uses=(IntendedUse.EXPRESSION,))
    with pytest.raises(ValidationError):
        build(transform_certificate_ref=REF)


def test_memory_access_request_necessity_is_exclusive() -> None:
    def build(necessity_text: str | None, necessity_payload_ref: str | None) -> MemoryAccessRequest:
        return MemoryAccessRequest(
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
            necessity_text=necessity_text,
            necessity_payload_ref=necessity_payload_ref,
            trigger_refs=(),
            runtime_origin=None,
        )

    with pytest.raises(ValidationError):
        build(necessity_text=None, necessity_payload_ref=None)
    with pytest.raises(ValidationError):
        build(necessity_text="why", necessity_payload_ref="payload:x")


def make_effect(**overrides: object) -> Effect:
    payload = {
        "schema_version": 2,
        "id": "effect:1",
        "revision": 1,
        "created_at": UTC,
        "logical_effect_id": "effect:1",
        "kind": "SEND_MESSAGE",
        "account_id": "account:agent",
        "destination_id": "room:public",
        "target_ref": None,
        "rendered_payload_ref": "payload:effect:1",
        "payload_digest": "0" * 64,
        "capability_ref": {"id": "capability:1", "revision": 1},
    }
    payload.update(overrides)
    result = record_from_payload(Effect, payload)
    assert isinstance(result, Effect)
    return result


def test_effect_plan_disposition_rules() -> None:
    def build(disposition: Disposition, effects: tuple[Effect, ...]) -> EffectPlan:
        return EffectPlan(
            id="plan:1",
            revision=1,
            created_at=UTC,
            intent_ref=RecordRef(id="intent:1", revision=1),
            snapshot_ref=RecordRef(id="snapshot:1", revision=1),
            disposition=disposition,
            effects=effects,
            proposal_revision=1,
            proposal_digest="0" * 64,
            defer_until=None,
            defer_expires_at=None,
            declared_fact_refs=(),
            observed_target_refs=(),
            producer_coverage_ref=RecordRef(id="coverage:1", revision=1),
        )

    effect = make_effect()
    with pytest.raises(ValidationError):
        build(disposition=Disposition.ACT, effects=())
    with pytest.raises(ValidationError):
        build(disposition=Disposition.QUIET, effects=(effect,))
    with pytest.raises(ValidationError):
        build(disposition=Disposition.DEFER, effects=())
    ok = build(disposition=Disposition.QUIET, effects=())
    assert ok.disposition == Disposition.QUIET


def test_add_reaction_requires_target() -> None:
    with pytest.raises(ValidationError):
        make_effect(kind=EffectKind.ADD_REACTION, target_ref=None)


# -- runtime-only discipline ---------------------------------------------------


def test_model_step_rejects_runtime_only_fields_anywhere() -> None:
    forged = {
        "kind": "SUBMIT_PLAN",
        "intent_ref": {"id": "intent:1", "revision": 1},
        "disposition": "ACT",
        "effects": [
            {
                "kind": "SEND_MESSAGE",
                "destination_id": "room:public",
                "text": "hello",
                "target_ref": None,
                "authorization": {"logical_effect_id": "effect:1"},
            }
        ],
        "declared_fact_refs": [],
    }
    with pytest.raises(ValidationError):
        parse_model_step(forged)
    with pytest.raises(ValidationError):
        parse_model_step({"kind": "FINISH", "outcome": "NO_ACTION", "runtime_epoch": 3})


def test_model_step_rejects_minted_identity_fields() -> None:
    with pytest.raises(ValidationError):
        parse_model_step(
            {
                "kind": "SUBMIT_PLAN",
                "intent_ref": {"id": "intent:1", "revision": 1},
                "disposition": "ACT",
                "effects": [
                    {
                        "kind": "SEND_MESSAGE",
                        "destination_id": "room:public",
                        "text": "hello",
                        "target_ref": None,
                        "logical_effect_id": "effect:self-minted",
                    }
                ],
                "declared_fact_refs": [],
            }
        )


def test_parse_model_step_submit_plan_happy_path() -> None:
    step = parse_model_step(
        {
            "kind": "SUBMIT_PLAN",
            "intent_ref": {"id": "intent:agent:public-research", "revision": 1},
            "disposition": "ACT",
            "effects": [
                {
                    "kind": "SEND_MESSAGE",
                    "destination_id": "room:public",
                    "text": "That reminds me of something useful from our project.",
                    "target_ref": None,
                }
            ],
            "declared_fact_refs": [],
        }
    )
    assert step.kind == MainStepKind.SUBMIT_PLAN
    assert isinstance(step, ModelSubmitPlanStep)
    assert step.effects[0].destination_id == "room:public"


def test_parse_model_step_request_access_bounds() -> None:
    step = parse_model_step(
        {
            "kind": "REQUEST_ACCESS",
            "request": {
                "request_id": "request:1",
                "resource_id": "resource:finance",
                "requested_precision": "FIELDS",
                "intended_use": "INTERNAL_ONLY",
                "necessity_text": "why",
            },
        }
    )
    assert step.kind == MainStepKind.REQUEST_ACCESS
    assert isinstance(step, ModelRequestAccessStep)
    with pytest.raises(ValidationError):
        parse_model_step(
            {
                "kind": "REQUEST_ACCESS",
                "request": {"request_id": "request:1", "runtime_origin": {}},
            }
        )


def test_parse_model_step_unknown_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_model_step({"kind": "THINK_HARDER"})


def test_parse_model_step_finish_choices() -> None:
    step = parse_model_step({"kind": "FINISH", "outcome": "NO_ACTION"})
    assert step.kind == MainStepKind.FINISH
    with pytest.raises(ValidationError):
        parse_model_step({"kind": "FINISH", "outcome": "MAYBE"})


def test_strict_json_loads_rejects_duplicate_keys() -> None:
    from agora.contracts import strict_json_loads

    assert strict_json_loads('{"a": 1}') == {"a": 1}
    with pytest.raises(ValidationError):
        strict_json_loads('{"a": 1, "a": 2}')


# -- round-trip parity over every record type ----------------------------------


def example_payloads() -> dict[str, dict[str, object]]:
    """One valid JSON payload per record type: the codec parity guard over
    the actual storage boundary."""
    ref = {"id": "principal:agent", "revision": 1}
    provenance = {
        "producer_ref": ref,
        "source_kind": "SOCIAL_MESSAGE",
        "origin_event_ref": {"id": "event:1", "revision": 1},
        "claimed_speaker_ref": ref,
        "instruction_authority": "NONE",
        "integrity": "EXTERNAL_UNVERIFIED",
        "custodian_refs": [ref],
        "subject_refs": [ref],
        "original_audience_ref": {"id": "audience:room", "revision": 1},
        "assertion_status": "OBSERVED_STATEMENT",
        "producer_coverage_ref": None,
        "model_claimed_parent_refs": [],
        "occurred_at": UTC,
        "observed_at": UTC,
    }
    vv = [{"id": "a:1", "revision": 1}, {"id": "b:1", "revision": 1}]
    header = lambda record_id: {  # noqa: E731
        "schema_version": 2,
        "id": record_id,
        "revision": 1,
        "created_at": UTC,
    }
    effect = {
        **header("effect:1"),
        "logical_effect_id": "effect:1",
        "kind": "SEND_MESSAGE",
        "account_id": "account:agent",
        "destination_id": "room:public",
        "target_ref": None,
        "rendered_payload_ref": "payload:effect:1",
        "payload_digest": "0" * 64,
        "capability_ref": {"id": "capability:1", "revision": 1},
    }
    return {
        "Principal": {
            **header("principal:agent"),
            "kind": "AGENT",
            "verified_bindings": [ref],
            "claimed_aliases": ["hermes"],
        },
        "AgentIdentity": {
            **header("identity:agent"),
            "principal_ref": ref,
            "preference_refs": [],
            "autobiography_refs": [],
            "commitment_refs": [],
            "active_interest_refs": [ref],
        },
        "AuthorityBinding": {
            **header("binding:1"),
            "issuer_ref": ref,
            "domain": "RESOURCE_USE",
            "resource_id": "resource:finance",
            "evidence_ref": ref,
            "delegated_from": None,
            "valid_until": None,
            "revoked_at": None,
        },
        "ResourcePolicy": {
            **header("policy:1"),
            "resource_id": "resource:finance",
            "data_class": "human_financial",
            "approval_rule": "SINGLE_ISSUER",
            "eligible_issuer_refs": [ref],
            "commitment_refs": [],
            "allowed_transform_ids": ["FIXED_FIELD_VIEW"],
            "policy_revision": 1,
            "status": "ACTIVE",
        },
        "UseAuthorization": {
            **header("useauth:1"),
            "resource_id": "resource:finance",
            "issuer_refs": [ref],
            "binding_refs": [ref],
            "operations": ["READ", "REVIEW"],
            "permitted_purpose_classes": ["social-support"],
            "audience_class_bounds": ["dm"],
            "maximum_precision": "FIELDS",
            "allowed_transform_ids": ["FIXED_FIELD_VIEW"],
            "valid_until": None,
            "status": "ACTIVE",
        },
        "CapabilityEnvelope": {
            **header("capability:1"),
            "actor_ref": ref,
            "account_id": "account:agent",
            "allowed_destination_ids": ["room:public"],
            "allowed_effect_kinds": ["SEND_MESSAGE", "ADD_REACTION"],
            "provisioning_binding_refs": [ref],
            "rate_budget_limits": [{"first": "per_minute", "second": 6}],
            "valid_until": None,
            "status": "ACTIVE",
        },
        "Commitment": {
            **header("commitment:1"),
            "subject_refs": [ref],
            "custodian_refs": [ref],
            "resource_id": "resource:finance",
            "source_scope": None,
            "strength": "STRONG",
            "internal_use_allowed": True,
            "observable_influence_allowed": False,
            "expression_allowed": False,
            "audience_predicate_id": "predicate:dm-only",
            "policy_evidence_refs": [ref],
        },
        "InboundEvent": {
            **header("event:1"),
            "source_event_id": "evt:1",
            "journal_seq": 1,
            "kind": "MESSAGE_CREATED",
            "actor_ref": ref,
            "occurred_at": UTC,
            "ingested_at": UTC,
            "source_audience_ref": {"id": "audience:room", "revision": 1},
            "fragment_refs": [{"id": "fragment:1", "revision": 1}],
            "reply_target_refs": [],
            "target_refs": [],
            "observed_trigger_refs": [],
            "recovery": "COMPLETE",
        },
        "AudienceSnapshot": {
            **header("audience:room"),
            "destination_id": "room:public",
            "environment_kind": "ROOM",
            "visibility": "PUBLIC",
            "known_principal_refs": [ref],
            "unknown_readers": True,
            "history_visibility": "PUBLIC_HISTORY",
            "completeness": "OBSERVED",
            "disclosure_context_revision": 1,
            "valid_until": None,
        },
        "ConversationEpisode": {
            **header("episode:1"),
            "container_id": "room:public",
            "member_event_refs": [{"id": "event:1", "revision": 1}],
            "reply_edges": [{"first": "event:1", "second": "event:2"}],
            "topic_refs": [],
            "summary_ref": None,
            "episode_revision": 1,
            "pending_intent_refs": [],
        },
        "ContentFragment": {
            **header("fragment:1"),
            "kind": "TEXT",
            "payload_ref": "payload:fragment:1",
            "provenance": provenance,
            "commitment_refs": [],
            "eligibility_status": "ACTIVE",
        },
        "ProducerCoverage": {
            **header("coverage:1"),
            "producer_kind": "CONTEXT_ASSEMBLY",
            "input_fragment_refs": [{"id": "fragment:1", "revision": 1}],
            "input_view_refs": [],
            "input_intent_refs": [],
            "input_policy_refs": [],
            "sink_ref": {"id": "sink:scripted", "revision": 1},
            "call_id": "call:1",
            "runtime_epoch": 1,
        },
        "Intent": {
            **header("intent:1"),
            "creator_ref": ref,
            "goal_fragment_ref": {"id": "fragment:goal", "revision": 1},
            "purpose_class": "social-support",
            "observed_trigger_refs": [{"id": "event:1", "revision": 1}],
            "endorsement_revision": 1,
            "producer_coverage_ref": None,
            "runtime_task_root_ids": ["root:1"],
            "parent_work_ref": None,
            "allowed_destination_refs": ["room:public"],
            "due_at": None,
            "expires_at": None,
            "state": "OPEN",
        },
        "WorkItem": {
            **header("work:turn:evt:1"),
            "intent_ref": {"id": "intent:1", "revision": 1},
            "trigger_key": "turn:evt:1",
            "episode_ref": None,
            "state": "READY",
            "lease_owner": None,
            "lease_until_ms": None,
            "runtime_epoch": 1,
            "main_steps_used": 0,
            "submissions_used": 0,
            "terminal_outcome_ref": None,
        },
        "MemoryRecord": {
            **header("memory:1"),
            "assertion_fragment_ref": {"id": "fragment:1", "revision": 1},
            "stage": "SOCIAL",
            "scope_kind": "ROOM",
            "scope_ref": "room:public",
            "custodian_refs": [ref],
            "subject_refs": [ref],
            "commitment_refs": [],
            "assertion_status": "OBSERVED_STATEMENT",
            "supersedes_ref": None,
            "valid_until": None,
            "status": "ACTIVE",
        },
        "Derivation": {
            **header("derivation:1"),
            "output_fragment_ref": {"id": "fragment:2", "revision": 1},
            "producer_coverage_ref": {"id": "coverage:1", "revision": 1},
            "claimed_parent_refs": [{"id": "fragment:1", "revision": 1}],
            "transform_id": None,
            "authorization_refs": [],
            "inherited_commitment_refs": [],
            "input_version_vector": vv,
            "state": "CURRENT",
        },
        "MemoryAccessRequest": {
            **header("request:1"),
            "request_id": "request:1",
            "intent_ref": {"id": "intent:1", "revision": 1},
            "resource_id": "resource:finance",
            "data_class": None,
            "requested_precision": "FIELDS",
            "requested_fields": ["balance"],
            "transform_id": None,
            "audience_ref": {"id": "audience:dm", "revision": 1},
            "intended_use": "INTERNAL_ONLY",
            "necessity_text": "decide whether to reply about budget",
            "necessity_payload_ref": None,
            "trigger_refs": [],
            "runtime_origin": {
                "actor_ref": ref,
                "work_ref": None,
                "snapshot_ref": None,
                "trigger_event_refs": [],
                "producer_coverage_ref": None,
                "runtime_epoch": 1,
            },
        },
        "MemoryAccessResult": {
            **header("result:1"),
            "request_id": "request:1",
            "access_status": "GRANTED",
            "view_kind": "PARTIAL",
            "view_ref": {"id": "view:1", "revision": 1},
            "reason_codes": [],
            "safe_feedback_template_id": None,
            "permitted_uses": ["INTERNAL_ONLY"],
            "producer_coverage_ref": {"id": "coverage:1", "revision": 1},
            "transform_certificate_ref": None,
            "authorization_refs": [{"id": "useauth:1", "revision": 1}],
            "inherited_commitment_refs": [],
        },
        "MemoryView": {
            **header("view:1"),
            "resource_id": "resource:finance",
            "returned_fragment_refs": [{"id": "fragment:9", "revision": 1}],
            "authorization_refs": [{"id": "useauth:1", "revision": 1}],
            "transform_certificate_ref": None,
            "producer_coverage_ref": {"id": "coverage:1", "revision": 1},
            "precision": "FIELDS",
            "inherited_commitment_refs": [],
            "permitted_uses": ["INTERNAL_ONLY"],
            "audience_bounds": ["dm"],
            "valid_until": None,
            "status": "ACTIVE",
        },
        "TransformCertificate": {
            **header("transform:1"),
            "registry_transform_id": "FIXED_FIELD_VIEW",
            "parameter_values": [{"first": "fields", "second": "balance"}],
            "source_coverage_ref": {"id": "coverage:1", "revision": 1},
            "authorizing_binding_refs": [ref],
            "authorizing_use_refs": [{"id": "useauth:1", "revision": 1}],
            "discharged_commitment_refs": [],
            "retained_commitment_refs": [{"id": "commitment:1", "revision": 1}],
            "output_view_ref": {"id": "view:1", "revision": 1},
        },
        "ComputeSink": {
            **header("sink:scripted"),
            "kind": "SCRIPTED_TEST",
            "configured_endpoint": None,
            "permitted_data_classes": ["public"],
            "capability_ref": None,
            "status": "ENABLED",
        },
        "ContextSnapshot": {
            **header("snapshot:1"),
            "identity_ref": {"id": "identity:agent", "revision": 1},
            "intent_ref": {"id": "intent:1", "revision": 1},
            "audience_ref": {"id": "audience:room", "revision": 1},
            "episode_ref": None,
            "selected_fragment_refs": [{"id": "fragment:1", "revision": 1}],
            "active_view_refs": [],
            "producer_coverage_ref": {"id": "coverage:1", "revision": 1},
            "exposure_class": "ORDINARY",
            "inherited_commitment_refs": [],
            "version_vector": vv,
            "sink_ref": {"id": "sink:scripted", "revision": 1},
            "effect_capable": True,
            "serializer_version": "ser:v1",
            "status": "ACTIVE",
        },
        "ModelCallEnvelope": {
            **header("envelope:1"),
            "snapshot_ref": {"id": "snapshot:1", "revision": 1},
            "purpose": "MAIN_STEP",
            "exact_messages_payload_ref": "payload:messages:1",
            "tool_schema_digest": "0" * 64,
            "supplied_fragment_refs": [{"id": "fragment:1", "revision": 1}],
            "supplied_view_refs": [],
            "sink_ref": {"id": "sink:scripted", "revision": 1},
            "request_digest": "0" * 64,
        },
        "PerceptionSignals": {
            **header("signals:1"),
            "event_refs": [{"id": "event:1", "revision": 1}],
            "window_refs": [],
            "probabilities": [{"first": "injection", "second": 0.25}],
            "address_flag": True,
            "reply_flag": False,
            "status": "KNOWN",
            "adapter_version": "perception:v1",
        },
        "Effect": effect,
        "EffectPlan": {
            **header("plan:1"),
            "intent_ref": {"id": "intent:1", "revision": 1},
            "snapshot_ref": {"id": "snapshot:1", "revision": 1},
            "disposition": "ACT",
            "effects": [effect],
            "proposal_revision": 1,
            "proposal_digest": "0" * 64,
            "defer_until": None,
            "defer_expires_at": None,
            "declared_fact_refs": [],
            "observed_target_refs": [],
            "producer_coverage_ref": {"id": "coverage:1", "revision": 1},
        },
        "ReviewRequirement": {
            **header("requirement:1"),
            "route": "REQUIRED",
            "reason_codes": [],
            "relevant_commitment_refs": [{"id": "commitment:1", "revision": 1}],
            "expected_coverage": ["FACT_COMPARISON"],
            "budget": 1,
            "sink_ref": {"id": "sink:scripted", "revision": 1},
            "policy_version_vector": vv,
        },
        "ReviewAssessment": {
            **header("assessment:plan:1:1"),
            "result": "PASS",
            "flags": [],
            "runtime_assigned_coverage": "FACT_COMPARISON",
            "supplied_view_refs": [],
            "proposal_digest": "0" * 64,
            "policy_version_vector": vv,
            "snapshot_revision": 1,
        },
        "CommitDecision": {
            **header("decision:1"),
            "kind": "AUTHORIZED",
            "reason_codes": [],
            "authorization_ref": {"id": "auth:1", "revision": 1},
            "assessment_ref": None,
        },
        "CommitAuthorization": {
            **header("auth:1"),
            "logical_effect_id": "effect:1",
            "proposal_revision": 1,
            "proposal_digest": "0" * 64,
            "payload_digest": "0" * 64,
            "identity_ref": {"id": "identity:agent", "revision": 1},
            "account_id": "account:agent",
            "audience_disclosure_revision": 1,
            "target_dependency_refs": [],
            "authority_version_vector": vv,
            "policy_version_vector": vv,
            "runtime_epoch": 1,
            "capability_ref": {"id": "capability:1", "revision": 1},
            "status": "ACTIVE",
        },
        "DispatchLease": {
            **header("lease:1"),
            "lease_id": "lease:1",
            "attempt_id": "attempt:1",
            "authorization_ref": {"id": "auth:1", "revision": 1},
            "logical_effect_id": "effect:1",
            "payload_digest": "0" * 64,
            "runtime_epoch": 1,
            "transport_generation": 1,
            "reservation_ref": None,
            "issued_at_ms": 1000,
            "expires_at_ms": 3000,
            "status": "ISSUED",
        },
        "DisclosureReservation": {
            **header("reservation:1"),
            "reservation_id": "res:1",
            "constraint_group_id": "group:finance",
            "subject_resource_refs": [{"id": "resource:finance", "revision": 1}],
            "audience_class": "dm",
            "transform_query_key": "affordability",
            "logical_effect_id": "effect:1",
            "state": "RESERVED",
        },
        "DeliveryReceipt": {
            **header("receipt:1"),
            "effect_id": "effect:1",
            "proposal_revision": 1,
            "attempt_id": "attempt:1",
            "state": "SENT",
            "fake_remote_id": "remote:1",
            "generation": 1,
            "error_code": None,
            "observed_at": UTC,
        },
        "TurnOutcome": {
            **header("outcome:1"),
            "kind": "COMMITTED",
            "intent_ref": {"id": "intent:1", "revision": 1},
            "work_ref": {"id": "work:turn:evt:1", "revision": 1},
            "reason_codes": [],
            "effect_refs": [{"id": "effect:1", "revision": 1}],
        },
        "ReviewEnvelope": {
            **header("reviewenvelope:1"),
            "plan_ref": {"id": "plan:1", "revision": 1},
            "proposal_digest": "0" * 64,
            "snapshot_ref": {"id": "snapshot:1", "revision": 1},
            "audience_ref": {"id": "audience:room", "revision": 1},
            "intent_ref": {"id": "intent:1", "revision": 1},
            "relevant_policy_refs": [],
            "relevant_commitment_refs": [{"id": "commitment:1", "revision": 1}],
            "comparison_view_refs": [],
            "bounded_quote_fragment_refs": [],
            "expected_coverage_refs": [{"id": "coverage:1", "revision": 1}],
            "sink_ref": {"id": "sink:scripted", "revision": 1},
            "exact_messages_payload_ref": "payload:messages:1",
        },
    }


@pytest.mark.parametrize("record_type", [cls.TYPE_NAME for cls in ALL_RECORD_TYPES])
def test_every_record_type_round_trips(record_type: str) -> None:
    payloads = example_payloads()
    assert set(payloads) == {cls.TYPE_NAME for cls in ALL_RECORD_TYPES}
    payload = payloads[record_type]
    record_cls = next(cls for cls in ALL_RECORD_TYPES if record_type == cls.TYPE_NAME)
    record = record_from_payload(record_cls, payload)
    encoded = record_to_payload(record)
    assert encoded == payload
    decoded_again = record_from_payload(record_cls, encoded)
    assert record_to_payload(decoded_again) == encoded
    canonical_json_bytes(encoded)
