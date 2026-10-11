"""Model-facing MainStep union and boundary parsing (handoff §4.3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agora.contracts.enums import (
    Disposition,
    EffectKind,
    FinishChoice,
    MainStepKind,
    MemoryStage,
    ParticipateMode,
    ScopeKind,
)
from agora.contracts.errors import ValidationError
from agora.contracts.values import (
    MAX_LIST_LEN,
    RecordRef,
    check_enum,
    check_id,
    check_str,
    check_utc,
    id_tuple,
    ref_tuple,
)

# ---------------------------------------------------------------------------
# Model-facing MainStep union (handoff §4.3)
# ---------------------------------------------------------------------------

MODEL_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        # runtime authority
        "authorization",
        "authorization_ref",
        "commit_authorization",
        "authority",
        "authority_binding",
        "use_authorization",
        "issuer_refs",
        # leases and reservations
        "lease",
        "lease_id",
        "dispatch_lease",
        "reservation",
        "reservation_ref",
        # epochs and generations
        "runtime_epoch",
        "transport_generation",
        "epoch",
        "generation",
        # runtime coverage
        "coverage",
        "producer_coverage",
        "producer_coverage_ref",
        "runtime_origin",
        # verified actors
        "actor_ref",
        "verified_actor",
        "creator_ref",
        # runtime-minted effect identity
        "capability_ref",
        "logical_effect_id",
        "proposal_revision",
        "proposal_digest",
        "payload_digest",
    }
)


def _each_object(value: object, path: str) -> Any:
    """Iterate a payload list, failing on non-object entries instead of skipping."""
    if not isinstance(value, list | tuple):
        raise ValidationError("expected a list", path=path)
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValidationError("expected an object entry", path=f"{path}[{index}]")
    return value


def _reject_forbidden_keys(value: object, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key in MODEL_FORBIDDEN_KEYS:
                raise ValidationError(
                    f"model-facing payload cannot carry runtime-only field {key!r}",
                    path=path,
                )
            _reject_forbidden_keys(item, f"{path}.{key}" if isinstance(key, str) else path)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_forbidden_keys(item, f"{path}[{index}]")


@dataclass(frozen=True, slots=True)
class ModelEffectDraft:
    """Model-selectable effect fields only (handoff §4.3 SUBMIT_PLAN shape)."""

    kind: EffectKind
    destination_id: str
    text: str | None
    target_ref: RecordRef | None
    emoji: str | None

    def __post_init__(self) -> None:
        check_enum(self.kind, EffectKind, "kind")
        check_id(self.destination_id, path="destination_id")
        if self.kind == EffectKind.SEND_MESSAGE:
            if self.text is None or self.emoji is not None:
                raise ValidationError("SEND_MESSAGE supplies text (no emoji)", path="text")
            check_str(self.text, "text")
        elif self.kind == EffectKind.ADD_REACTION:
            if self.emoji is None or self.text is not None or self.target_ref is None:
                raise ValidationError(
                    "ADD_REACTION supplies emoji and target_ref (no text)", path="emoji"
                )
            check_str(self.emoji, "emoji", max_chars=16)


@dataclass(frozen=True, slots=True)
class ModelParticipateStep:
    kind: MainStepKind
    mode: ParticipateMode
    interest_updates: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.kind != MainStepKind.PARTICIPATE:
            raise ValidationError("kind must be PARTICIPATE", path="kind")
        check_enum(self.mode, ParticipateMode, "mode")
        if len(self.interest_updates) > MAX_LIST_LEN:
            raise ValidationError(f"list exceeds bound of {MAX_LIST_LEN}", path="interest_updates")
        for update in self.interest_updates:
            check_str(update, "interest_updates")


@dataclass(frozen=True, slots=True)
class ModelRequestAccessStep:
    kind: MainStepKind
    request: dict[str, Any]

    def __post_init__(self) -> None:
        if self.kind != MainStepKind.REQUEST_ACCESS:
            raise ValidationError("kind must be REQUEST_ACCESS", path="kind")
        if not isinstance(self.request, dict):
            raise ValidationError("request must be an object", path="request")
        allowed = {
            "request_id",
            "resource_id",
            "data_class",
            "requested_precision",
            "requested_fields",
            "transform_id",
            "audience_ref",
            "intended_use",
            "necessity_text",
            "trigger_refs",
        }
        unknown = set(self.request) - allowed
        if unknown:
            raise ValidationError(
                f"request carries fields outside the model-selectable set: {sorted(unknown)}",
                path="request",
            )


@dataclass(frozen=True, slots=True)
class ModelProposeMemoryStep:
    kind: MainStepKind
    assertion_text: str
    source_refs: tuple[RecordRef, ...]
    requested_stage: MemoryStage
    requested_scope_kind: ScopeKind
    scope_ref: str
    existing_memory_ref: RecordRef | None

    def __post_init__(self) -> None:
        if self.kind != MainStepKind.PROPOSE_MEMORY:
            raise ValidationError("kind must be PROPOSE_MEMORY", path="kind")
        check_str(self.assertion_text, "assertion_text")
        ref_tuple(self.source_refs, "source_refs")
        check_enum(self.requested_stage, MemoryStage, "requested_stage")
        check_enum(self.requested_scope_kind, ScopeKind, "requested_scope_kind")
        check_id(self.scope_ref, path="scope_ref")


@dataclass(frozen=True, slots=True)
class ModelAdoptPreferenceStep:
    kind: MainStepKind
    preference_statement: str

    def __post_init__(self) -> None:
        if self.kind != MainStepKind.ADOPT_PREFERENCE:
            raise ValidationError("kind must be ADOPT_PREFERENCE", path="kind")
        check_str(self.preference_statement, "preference_statement")


@dataclass(frozen=True, slots=True)
class ModelCreateIntentStep:
    kind: MainStepKind
    goal_text: str
    purpose_class: str
    allowed_destination_ids: tuple[str, ...]
    due_at: str | None
    expires_at: str | None

    def __post_init__(self) -> None:
        if self.kind != MainStepKind.CREATE_INTENT:
            raise ValidationError("kind must be CREATE_INTENT", path="kind")
        check_str(self.goal_text, "goal_text")
        check_id(self.purpose_class, path="purpose_class")
        id_tuple(self.allowed_destination_ids, "allowed_destination_ids")
        if self.due_at is not None:
            check_utc(self.due_at, path="due_at")
        if self.expires_at is not None:
            check_utc(self.expires_at, path="expires_at")


@dataclass(frozen=True, slots=True)
class ModelSubmitPlanStep:
    kind: MainStepKind
    intent_ref: RecordRef
    disposition: Disposition
    effects: tuple[ModelEffectDraft, ...]
    declared_fact_refs: tuple[RecordRef, ...]

    def __post_init__(self) -> None:
        if self.kind != MainStepKind.SUBMIT_PLAN:
            raise ValidationError("kind must be SUBMIT_PLAN", path="kind")
        if not isinstance(self.intent_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="intent_ref")
        check_enum(self.disposition, Disposition, "disposition")
        if not isinstance(self.effects, tuple) or len(self.effects) > 1:
            raise ValidationError("at most one effect per plan in this slice", path="effects")
        for effect in self.effects:
            if not isinstance(effect, ModelEffectDraft):
                raise ValidationError("expected a ModelEffectDraft", path="effects")
        if self.disposition == Disposition.ACT and len(self.effects) != 1:
            raise ValidationError("ACT carries exactly one effect", path="effects")
        if self.disposition != Disposition.ACT and self.effects:
            raise ValidationError("QUIET and DEFER carry zero effects", path="effects")
        ref_tuple(self.declared_fact_refs, "declared_fact_refs")


@dataclass(frozen=True, slots=True)
class ModelFinishStep:
    kind: MainStepKind
    outcome: FinishChoice

    def __post_init__(self) -> None:
        if self.kind != MainStepKind.FINISH:
            raise ValidationError("kind must be FINISH", path="kind")
        check_enum(self.outcome, FinishChoice, "outcome")


MainStep = (
    ModelParticipateStep
    | ModelRequestAccessStep
    | ModelProposeMemoryStep
    | ModelAdoptPreferenceStep
    | ModelCreateIntentStep
    | ModelSubmitPlanStep
    | ModelFinishStep
)


_REQUIRED_STEP_FIELDS: dict[MainStepKind, tuple[str, ...]] = {
    MainStepKind.PARTICIPATE: ("mode",),
    MainStepKind.REQUEST_ACCESS: ("request",),
    MainStepKind.PROPOSE_MEMORY: (
        "assertion_text",
        "requested_stage",
        "requested_scope_kind",
        "scope_ref",
    ),
    MainStepKind.ADOPT_PREFERENCE: ("preference_statement",),
    MainStepKind.CREATE_INTENT: ("goal_text", "purpose_class", "allowed_destination_ids"),
    MainStepKind.SUBMIT_PLAN: ("intent_ref", "disposition", "effects"),
    MainStepKind.FINISH: ("outcome",),
}


def _require_step_fields(data: dict[str, Any], kind: MainStepKind) -> None:
    for field_name in _REQUIRED_STEP_FIELDS[kind]:
        if field_name not in data:
            raise ValidationError(f"{kind.value} requires field {field_name!r}", path=field_name)


def parse_model_step(data: object) -> MainStep:
    """Parse a model-facing decision payload into a typed MainStep.

    Runtime-only fields are rejected anywhere in the payload: a fabricated
    CommitAuthorization inside a model response is invalid by construction
    (handoff §3).
    """
    if not isinstance(data, dict):
        raise ValidationError("model step must be an object", path="step")
    _reject_forbidden_keys(data)
    kind = data.get("kind")
    if not isinstance(kind, str):
        raise ValidationError("model step requires a kind", path="kind")
    try:
        step_kind = MainStepKind(kind)
    except ValueError:
        raise ValidationError(f"unknown step kind {kind!r}", path="kind") from None
    _require_step_fields(data, step_kind)
    if step_kind is MainStepKind.PARTICIPATE:
        return ModelParticipateStep(
            kind=step_kind,
            mode=check_enum(data.get("mode"), ParticipateMode, "mode"),
            interest_updates=tuple(data.get("interest_updates", ())),
        )
    if step_kind is MainStepKind.REQUEST_ACCESS:
        return ModelRequestAccessStep(kind=step_kind, request=data.get("request", {}))
    if step_kind is MainStepKind.PROPOSE_MEMORY:
        return ModelProposeMemoryStep(
            kind=step_kind,
            assertion_text=data["assertion_text"],
            source_refs=tuple(RecordRef.from_payload(item) for item in data.get("source_refs", ())),
            requested_stage=check_enum(data.get("requested_stage"), MemoryStage, "requested_stage"),
            requested_scope_kind=check_enum(
                data.get("requested_scope_kind"), ScopeKind, "requested_scope_kind"
            ),
            scope_ref=data["scope_ref"],
            existing_memory_ref=RecordRef.from_payload(data["existing_memory_ref"])
            if data.get("existing_memory_ref")
            else None,
        )
    if step_kind is MainStepKind.ADOPT_PREFERENCE:
        return ModelAdoptPreferenceStep(
            kind=step_kind, preference_statement=data["preference_statement"]
        )
    if step_kind is MainStepKind.CREATE_INTENT:
        return ModelCreateIntentStep(
            kind=step_kind,
            goal_text=data["goal_text"],
            purpose_class=data["purpose_class"],
            allowed_destination_ids=tuple(data.get("allowed_destination_ids", ())),
            due_at=data.get("due_at"),
            expires_at=data.get("expires_at"),
        )
    if step_kind is MainStepKind.SUBMIT_PLAN:
        return ModelSubmitPlanStep(
            kind=step_kind,
            intent_ref=RecordRef.from_payload(data.get("intent_ref")),
            disposition=check_enum(data.get("disposition"), Disposition, "disposition"),
            effects=tuple(
                ModelEffectDraft(
                    kind=check_enum(effect.get("kind"), EffectKind, "effects.kind"),
                    destination_id=effect.get("destination_id", ""),
                    text=effect.get("text"),
                    target_ref=RecordRef.from_payload(effect["target_ref"])
                    if effect.get("target_ref")
                    else None,
                    emoji=effect.get("emoji"),
                )
                for effect in _each_object(data.get("effects", ()), "effects")
            ),
            declared_fact_refs=tuple(
                RecordRef.from_payload(item) for item in data.get("declared_fact_refs", ())
            ),
        )
    return ModelFinishStep(
        kind=step_kind, outcome=check_enum(data.get("outcome"), FinishChoice, "outcome")
    )
