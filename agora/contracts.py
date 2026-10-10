"""Typed domain contracts for the synthetic replay runtime.

Owns the handoff v0.2 §3 common record rules, the §4 record set, canonical
payload binding, and the model-facing MainStep union. This module owns no
policy decisions, no database access, and no model calls (handoff §2).

Payload references (`*_payload_ref`) are plain bounded string ids into the
out-of-line payload stores, not RecordRefs: payloads are blobs, not
revisioned records. References to revisioned content are always RecordRef.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import types
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any, ClassVar, TypeVar, get_args, get_origin, get_type_hints

E = TypeVar("E", bound=StrEnum)

RECORD_SCHEMA_VERSION = 2

MAX_ID_LEN = 128
MAX_LIST_LEN = 64
MAX_TEXT_CHARS = 2000
MAX_PAYLOAD_BYTES = 65536

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")
RFC3339_UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ContractError(Exception):
    """Base class for contract-layer failures."""


class ValidationError(ContractError):
    """A value or mapping failed contract validation.

    ``code`` is a ReasonCode string (INVALID_SCHEMA for shape failures,
    INVALID_REFERENCE for typed-reference failures); ``path`` is the dotted
    field path where the failure was detected.
    """

    def __init__(self, message: str, *, code: str = "INVALID_SCHEMA", path: str = "") -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.code = code
        self.path = path


class CanonicalizationError(ContractError):
    """A value cannot be canonically bound."""


# ---------------------------------------------------------------------------
# Safe reason-code registry (handoff §4.4) — closed; extension is an explicit
# schema change with tests.
# ---------------------------------------------------------------------------


class ReasonCode(StrEnum):
    UNSUPPORTED = "UNSUPPORTED"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    INVALID_REFERENCE = "INVALID_REFERENCE"
    AUTHORITY_MISSING = "AUTHORITY_MISSING"
    AUTHORITY_CONFLICT = "AUTHORITY_CONFLICT"
    POLICY_FORBIDDEN = "POLICY_FORBIDDEN"
    POLICY_CHANGED = "POLICY_CHANGED"
    VIEW_FORBIDDEN = "VIEW_FORBIDDEN"
    SINK_FORBIDDEN = "SINK_FORBIDDEN"
    CONTEXT_MISMATCH = "CONTEXT_MISMATCH"
    STALE_CONTEXT = "STALE_CONTEXT"
    AUDIENCE_CHANGED = "AUDIENCE_CHANGED"
    TARGET_CHANGED = "TARGET_CHANGED"
    CAPABILITY_INVALID = "CAPABILITY_INVALID"
    REVIEW_CONCERN = "REVIEW_CONCERN"
    REVIEW_FACTS_MISSING = "REVIEW_FACTS_MISSING"
    REVIEW_TIMEOUT = "REVIEW_TIMEOUT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    RELEASE_LIMIT = "RELEASE_LIMIT"
    STALE_EPOCH = "STALE_EPOCH"
    STALE_GENERATION = "STALE_GENERATION"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    LEASE_CONSUMED = "LEASE_CONSUMED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED_BEFORE_DISPATCH = "CANCELLED_BEFORE_DISPATCH"
    TOO_LATE = "TOO_LATE"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    CONFIRMED_NO_EFFECT = "CONFIRMED_NO_EFFECT"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
    NO_ACTION = "NO_ACTION"
    DEFERRED = "DEFERRED"
    COMMITTED = "COMMITTED"


REASON_CODES: frozenset[str] = frozenset(ReasonCode)


# ---------------------------------------------------------------------------
# Closed vocabularies (handoff §4). Unknown values fail validation; future
# kinds are new typed members, never reuse of an existing one.
# ---------------------------------------------------------------------------


class EventKind(StrEnum):
    MESSAGE_CREATED = "MESSAGE_CREATED"
    MESSAGE_UPDATED = "MESSAGE_UPDATED"
    MESSAGE_DELETED = "MESSAGE_DELETED"
    AUDIENCE_CHANGED = "AUDIENCE_CHANGED"
    POLICY_CHANGED = "POLICY_CHANGED"
    TIMER_DUE = "TIMER_DUE"
    AGENT_INTENT_CREATED = "AGENT_INTENT_CREATED"
    AGENT_STATE_ADOPTED = "AGENT_STATE_ADOPTED"
    CONNECTION_GAP = "CONNECTION_GAP"


class RecoveryStatus(StrEnum):
    COMPLETE = "COMPLETE"
    GAP = "GAP"
    UNKNOWN = "UNKNOWN"


class PrincipalKind(StrEnum):
    AGENT = "AGENT"
    HUMAN_OPERATOR = "HUMAN_OPERATOR"
    THIRD_PARTY = "THIRD_PARTY"


class AuthorityDomain(StrEnum):
    RESOURCE_USE = "RESOURCE_USE"
    DISCLOSURE = "DISCLOSURE"
    AGENT_STATE = "AGENT_STATE"
    DEPLOYMENT = "DEPLOYMENT"


class ApprovalRule(StrEnum):
    SINGLE_ISSUER = "SINGLE_ISSUER"
    ALL_LISTED = "ALL_LISTED"


class RecordStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class ResourceOperation(StrEnum):
    READ = "READ"
    TRANSFORM = "TRANSFORM"
    REVIEW = "REVIEW"
    STORE = "STORE"
    EXPRESS = "EXPRESS"
    OBSERVABLE_INFLUENCE = "OBSERVABLE_INFLUENCE"


class CommitmentStrength(StrEnum):
    ORDINARY_DISCRETION = "ORDINARY_DISCRETION"
    STRONG = "STRONG"


class EligibilityStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    TOMBSTONED = "TOMBSTONED"


class EnvironmentKind(StrEnum):
    ROOM = "ROOM"
    DM = "DM"
    INTERNAL = "INTERNAL"


class Visibility(StrEnum):
    PUBLIC = "PUBLIC"
    RESTRICTED = "RESTRICTED"
    PRIVATE_DM = "PRIVATE_DM"
    AGENT_PRIVATE = "AGENT_PRIVATE"


class HistoryVisibility(StrEnum):
    PUBLIC_HISTORY = "PUBLIC_HISTORY"
    RESTRICTED_HISTORY = "RESTRICTED_HISTORY"
    DM_PARTICIPANTS = "DM_PARTICIPANTS"
    UNKNOWN = "UNKNOWN"


class Completeness(StrEnum):
    OBSERVED = "OBSERVED"
    INCOMPLETE = "INCOMPLETE"


class FragmentKind(StrEnum):
    TEXT = "TEXT"
    STRUCTURED = "STRUCTURED"
    MODEL_OUTPUT = "MODEL_OUTPUT"
    SUMMARY = "SUMMARY"


class SourceKind(StrEnum):
    FIXTURE_CONTROL = "FIXTURE_CONTROL"
    SOCIAL_MESSAGE = "SOCIAL_MESSAGE"
    WEB_QUOTE = "WEB_QUOTE"
    TOOL_RESULT = "TOOL_RESULT"
    AGENT_OUTPUT = "AGENT_OUTPUT"
    MEMORY = "MEMORY"
    SUMMARY = "SUMMARY"
    INFERENCE = "INFERENCE"


class InstructionAuthority(StrEnum):
    CONTROL_PLANE = "CONTROL_PLANE"
    AGENT_STATE = "AGENT_STATE"
    NONE = "NONE"


class Integrity(StrEnum):
    RUNTIME_VERIFIED = "RUNTIME_VERIFIED"
    EXTERNAL_UNVERIFIED = "EXTERNAL_UNVERIFIED"
    DERIVED = "DERIVED"


class AssertionStatus(StrEnum):
    OBSERVED_STATEMENT = "OBSERVED_STATEMENT"
    ATTRIBUTED_ASSERTION = "ATTRIBUTED_ASSERTION"
    CONTESTED = "CONTESTED"
    INFERENCE = "INFERENCE"
    AGENT_ADOPTED_PREFERENCE = "AGENT_ADOPTED_PREFERENCE"


class ProducerKind(StrEnum):
    CONTEXT_ASSEMBLY = "CONTEXT_ASSEMBLY"
    MAIN = "MAIN"
    REVIEW = "REVIEW"
    SUMMARY = "SUMMARY"
    TRANSFORM = "TRANSFORM"


class IntentState(StrEnum):
    OPEN = "OPEN"
    DEFERRED = "DEFERRED"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


class WorkState(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    HOLD = "HOLD"
    COMPLETED = "COMPLETED"


class MemoryStage(StrEnum):
    EPHEMERAL = "EPHEMERAL"
    SOCIAL = "SOCIAL"
    CANDIDATE = "CANDIDATE"
    DURABLE = "DURABLE"


class ScopeKind(StrEnum):
    AGENT = "AGENT"
    RELATIONSHIP = "RELATIONSHIP"
    ROOM = "ROOM"
    PROJECT = "PROJECT"
    BROAD = "BROAD"


class MemoryStatus(StrEnum):
    ACTIVE = "ACTIVE"
    STALE = "STALE"
    TOMBSTONED = "TOMBSTONED"


class DerivationState(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"


class RequestedPrecision(StrEnum):
    EXACT = "EXACT"
    FIELDS = "FIELDS"
    ABSTRACT = "ABSTRACT"


class IntendedUse(StrEnum):
    INTERNAL_ONLY = "INTERNAL_ONLY"
    OBSERVABLE_INFLUENCE = "OBSERVABLE_INFLUENCE"
    EXPRESSION = "EXPRESSION"


class AccessStatus(StrEnum):
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    PENDING_AUTHORITY = "PENDING_AUTHORITY"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICT = "CONFLICT"


class ViewKind(StrEnum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    ABSTRACTED = "ABSTRACTED"
    NONE = "NONE"


class SinkKind(StrEnum):
    SCRIPTED_TEST = "SCRIPTED_TEST"
    LOCAL_MODEL = "LOCAL_MODEL"
    REMOTE_UNSUPPORTED = "REMOTE_UNSUPPORTED"


class SinkStatus(StrEnum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class EnvelopePurpose(StrEnum):
    MAIN_STEP = "MAIN_STEP"
    REVIEW = "REVIEW"
    SUMMARY = "SUMMARY"


class ExposureClass(StrEnum):
    ORDINARY = "ORDINARY"
    STRONG = "STRONG"


class SnapshotStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INVALID = "INVALID"


class PerceptionStatus(StrEnum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


class Disposition(StrEnum):
    ACT = "ACT"
    QUIET = "QUIET"
    DEFER = "DEFER"


class EffectKind(StrEnum):
    SEND_MESSAGE = "SEND_MESSAGE"
    ADD_REACTION = "ADD_REACTION"


class ReviewRoute(StrEnum):
    NONE = "NONE"
    ADVISORY = "ADVISORY"
    REQUIRED = "REQUIRED"
    HARD_DENY = "HARD_DENY"


class AssessmentResult(StrEnum):
    PASS = "PASS"
    CONCERNS = "CONCERNS"
    INSUFFICIENT_CONTEXT = "INSUFFICIENT_CONTEXT"


class ReviewCoverage(StrEnum):
    FACT_COMPARISON = "FACT_COMPARISON"
    DESCRIPTORS_ONLY = "DESCRIPTORS_ONLY"
    NO_FACT_ACCESS = "NO_FACT_ACCESS"


class DecisionKind(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    HOLD = "HOLD"
    DENIED = "DENIED"
    QUIET_COMPLETED = "QUIET_COMPLETED"
    DEFERRED = "DEFERRED"


class AuthorizationStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"


class LeaseStatus(StrEnum):
    ISSUED = "ISSUED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class ReservationState(StrEnum):
    RESERVED = "RESERVED"
    COMMITTED = "COMMITTED"
    POSSIBLY_COMMITTED = "POSSIBLY_COMMITTED"
    CANCELLED_BEFORE_DISPATCH = "CANCELLED_BEFORE_DISPATCH"


class DeliveryState(StrEnum):
    SENT = "SENT"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"
    UNKNOWN = "UNKNOWN"


class OutcomeKind(StrEnum):
    NO_ACTION = "NO_ACTION"
    DEFERRED = "DEFERRED"
    COMMITTED = "COMMITTED"
    DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
    HELD = "HELD"
    FAILED_INTERNAL = "FAILED_INTERNAL"


class ParticipateMode(StrEnum):
    ENGAGE = "ENGAGE"
    REACT = "REACT"
    IGNORE = "IGNORE"
    EXPLORE = "EXPLORE"


class MainStepKind(StrEnum):
    PARTICIPATE = "PARTICIPATE"
    REQUEST_ACCESS = "REQUEST_ACCESS"
    PROPOSE_MEMORY = "PROPOSE_MEMORY"
    ADOPT_PREFERENCE = "ADOPT_PREFERENCE"
    CREATE_INTENT = "CREATE_INTENT"
    SUBMIT_PLAN = "SUBMIT_PLAN"
    FINISH = "FINISH"


class FinishChoice(StrEnum):
    NO_ACTION = "NO_ACTION"
    DEFER = "DEFER"


class DependencyKind(StrEnum):
    PRODUCER_INPUT = "PRODUCER_INPUT"
    DECLARED = "DECLARED"
    CONTROL = "CONTROL"


class PayloadStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PURGED = "PURGED"


# ---------------------------------------------------------------------------
# Shared validators
# ---------------------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_str(value: object, path: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"expected string, got {type(value).__name__}", path=path)
    if len(value) > max_chars:
        raise ValidationError(f"string exceeds {max_chars} characters", path=path)
    return value


def check_id(value: object, path: str = "id") -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValidationError(
            f"id must match {ID_PATTERN.pattern} (opaque, <= {MAX_ID_LEN} chars)", path=path
        )
    return value


def check_positive_int(value: object, path: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    raise ValidationError("expected a positive integer", path=path)


def check_non_negative_int(value: object, path: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ValidationError("expected a non-negative integer", path=path)


def check_bool(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError("expected a boolean", path=path)
    return value


def check_utc(value: object, path: str) -> str:
    if not isinstance(value, str) or not RFC3339_UTC_PATTERN.fullmatch(value):
        raise ValidationError(
            "expected UTC RFC3339 timestamp with Z suffix (e.g. 2026-01-01T00:00:00Z)", path=path
        )
    return value


def check_sha256(value: object, path: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ValidationError("expected a lowercase SHA-256 hex digest", path=path)
    return value


def check_enum[E: StrEnum](value: object, enum_cls: type[E], path: str) -> E:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            pass
    raise ValidationError(
        f"expected one of {', '.join(m.value for m in enum_cls)}; got {value!r}", path=path
    )


def check_finite_float(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValidationError("expected a number", path=path)
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValidationError("expected a finite number", path=path)
    return number


# ---------------------------------------------------------------------------
# Canonical payload binding (handoff §3)
# ---------------------------------------------------------------------------


def _reject_unbindable(value: object, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"{path}: object keys must be strings")
            _reject_unbindable(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_unbindable(item, f"{path}[{index}]")
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise CanonicalizationError(f"{path}: non-finite numeric value")
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise CanonicalizationError(f"{path}: unpaired surrogate in string") from None
    elif value is not None and not isinstance(value, bool | int):
        raise CanonicalizationError(f"{path}: value of type {type(value).__name__} is not bindable")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate object key {key!r}", path=key)
        result[key] = value
    return result


def strict_json_loads(text: str | bytes) -> Any:
    """Parse JSON rejecting duplicate keys (json.loads silently last-wins)."""
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON: {exc}") from exc


def canonical_json_bytes(value: object) -> bytes:
    """Canonical binding bytes: sorted keys, no whitespace, finite-only, UTF-8."""
    _reject_unbindable(value)
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (UnicodeEncodeError, ValueError) as exc:
        raise CanonicalizationError(f"value cannot be canonically encoded: {exc}") from exc


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def render_send_message(
    *, destination_id: str, text: str, reply_target_ref: RecordRef | None
) -> dict[str, Any]:
    """Exact rendered SEND_MESSAGE payload shape (handoff §4.4)."""
    return {
        "kind": "SEND_MESSAGE",
        "destination_id": destination_id,
        "text": text,
        "reply_target_ref": (
            {"id": reply_target_ref.id, "revision": reply_target_ref.revision}
            if reply_target_ref is not None
            else None
        ),
    }


def render_add_reaction(
    *,
    destination_id: str,
    target_ref: RecordRef,
    target_content_digest: str,
    emoji: str,
) -> dict[str, Any]:
    """Exact rendered ADD_REACTION payload shape (handoff §4.4)."""
    return {
        "kind": "ADD_REACTION",
        "destination_id": destination_id,
        "target_ref": {"id": target_ref.id, "revision": target_ref.revision},
        "target_content_digest": target_content_digest,
        "emoji": emoji,
    }


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordRef:
    """Reference to revisioned content: exactly an id and a positive revision."""

    id: str
    revision: int

    def __post_init__(self) -> None:
        check_id(self.id, path="id")
        check_positive_int(self.revision, path="revision")

    def to_payload(self) -> dict[str, Any]:
        return {"id": self.id, "revision": self.revision}

    @classmethod
    def from_payload(cls, data: object) -> RecordRef:
        if not isinstance(data, dict) or set(data) != {"id", "revision"}:
            raise ValidationError("RecordRef payload must be exactly {id, revision}", path="ref")
        return cls(id=data["id"], revision=data["revision"])


@dataclass(frozen=True, slots=True)
class Pair:
    """A bounded label/value entry (rate limits, transform parameters, edges).

    Order semantics belong to the field, not the pair: unordered label-keyed
    fields sort at validation; ordered fields (e.g. reply edges) keep order.
    """

    first: str
    second: str | int | float | bool | None

    def __post_init__(self) -> None:
        check_str(self.first, path="first", max_chars=MAX_ID_LEN)
        if isinstance(self.second, float):
            check_finite_float(self.second, path="second")
        elif self.second is not None and not isinstance(self.second, bool | int | str):
            raise ValidationError(
                "pair value must be a string, number, boolean, or null", path="second"
            )

    def to_payload(self) -> dict[str, Any]:
        return {"first": self.first, "second": self.second}

    @classmethod
    def from_payload(cls, data: object) -> Pair:
        if not isinstance(data, dict) or set(data) != {"first", "second"}:
            raise ValidationError("Pair payload must be exactly {first, second}", path="pair")
        return cls(first=data["first"], second=data["second"])


def ref_tuple(value: object, path: str) -> tuple[RecordRef, ...]:
    """Validate a bounded tuple of RecordRefs."""
    if not isinstance(value, tuple):
        raise ValidationError("expected a tuple of refs", path=path)
    if len(value) > MAX_LIST_LEN:
        raise ValidationError(f"list exceeds bound of {MAX_LIST_LEN}", path=path)
    return tuple(
        item if isinstance(item, RecordRef) else RecordRef.from_payload(item) for item in value
    )


def id_tuple(value: object, path: str, *, max_chars: int = MAX_ID_LEN) -> tuple[str, ...]:
    """Validate a bounded tuple of opaque ids."""
    if not isinstance(value, tuple):
        raise ValidationError("expected a tuple of ids", path=path)
    if len(value) > MAX_LIST_LEN:
        raise ValidationError(f"list exceeds bound of {MAX_LIST_LEN}", path=path)
    for item in value:
        check_id(item, path=path)
    return value


def enum_tuple[E: StrEnum](value: object, enum_cls: type[E], path: str) -> tuple[E, ...]:
    """Validate a bounded tuple of closed-vocabulary values."""
    if not isinstance(value, tuple):
        raise ValidationError(f"expected a tuple of {enum_cls.__name__} values", path=path)
    if len(value) > MAX_LIST_LEN:
        raise ValidationError(f"list exceeds bound of {MAX_LIST_LEN}", path=path)
    return tuple(check_enum(item, enum_cls, path=path) for item in value)


def pair_tuple(value: object, path: str) -> tuple[Pair, ...]:
    if not isinstance(value, tuple):
        raise ValidationError("expected a tuple of pairs", path=path)
    if len(value) > MAX_LIST_LEN:
        raise ValidationError(f"list exceeds bound of {MAX_LIST_LEN}", path=path)
    for item in value:
        if not isinstance(item, Pair):
            raise ValidationError("expected a Pair value", path=path)
    return value


def sorted_pair_tuple(value: object, path: str) -> tuple[Pair, ...]:
    """Label-keyed pairs: semantically unordered, so sorted by label (§3)."""
    pairs = pair_tuple(value, path)
    labels = [pair.first for pair in pairs]
    if len(set(labels)) != len(labels):
        raise ValidationError("duplicate label in label-keyed pairs", path=path)
    return tuple(sorted(pairs, key=lambda pair: pair.first))


def check_version_vector(value: object, path: str) -> tuple[RecordRef, ...]:
    """Version vector: sorted list of RecordRefs, at most one revision per id."""
    refs = ref_tuple(value, path)
    ids = [ref.id for ref in refs]
    if len(set(ids)) != len(ids):
        raise ValidationError("version vector repeats an id", path=path)
    return tuple(sorted(refs, key=lambda ref: ref.id))


@dataclass(frozen=True, slots=True)
class RuntimeOrigin:
    """Runtime-populated origin record; the model never sets these fields."""

    actor_ref: RecordRef
    work_ref: RecordRef | None
    snapshot_ref: RecordRef | None
    trigger_event_refs: tuple[RecordRef, ...]
    producer_coverage_ref: RecordRef | None
    runtime_epoch: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "trigger_event_refs", ref_tuple(self.trigger_event_refs, "trigger_event_refs")
        )
        check_non_negative_int(self.runtime_epoch, path="runtime_epoch")

    def to_payload(self) -> dict[str, Any]:
        return {
            "actor_ref": self.actor_ref.to_payload(),
            "work_ref": self.work_ref.to_payload() if self.work_ref else None,
            "snapshot_ref": self.snapshot_ref.to_payload() if self.snapshot_ref else None,
            "trigger_event_refs": [ref.to_payload() for ref in self.trigger_event_refs],
            "producer_coverage_ref": (
                self.producer_coverage_ref.to_payload() if self.producer_coverage_ref else None
            ),
            "runtime_epoch": self.runtime_epoch,
        }

    @classmethod
    def from_payload(cls, data: object) -> RuntimeOrigin:
        if not isinstance(data, dict):
            raise ValidationError("RuntimeOrigin payload must be an object", path="runtime_origin")
        expected = {
            "actor_ref",
            "work_ref",
            "snapshot_ref",
            "trigger_event_refs",
            "producer_coverage_ref",
            "runtime_epoch",
        }
        if set(data) != expected:
            raise ValidationError(
                f"RuntimeOrigin fields must be exactly {sorted(expected)}", path="runtime_origin"
            )
        return cls(
            actor_ref=RecordRef.from_payload(data["actor_ref"]),
            work_ref=RecordRef.from_payload(data["work_ref"]) if data["work_ref"] else None,
            snapshot_ref=RecordRef.from_payload(data["snapshot_ref"])
            if data["snapshot_ref"]
            else None,
            trigger_event_refs=tuple(
                RecordRef.from_payload(item) for item in data["trigger_event_refs"]
            ),
            producer_coverage_ref=(
                RecordRef.from_payload(data["producer_coverage_ref"])
                if data["producer_coverage_ref"]
                else None
            ),
            runtime_epoch=data["runtime_epoch"],
        )


@dataclass(frozen=True, slots=True)
class Provenance:
    """Embedded provenance value object (handoff §4.1)."""

    producer_ref: RecordRef
    source_kind: SourceKind
    origin_event_ref: RecordRef | None
    claimed_speaker_ref: RecordRef | None
    instruction_authority: InstructionAuthority
    integrity: Integrity
    custodian_refs: tuple[RecordRef, ...]
    subject_refs: tuple[RecordRef, ...]
    original_audience_ref: RecordRef | None
    assertion_status: AssertionStatus
    producer_coverage_ref: RecordRef | None
    model_claimed_parent_refs: tuple[RecordRef, ...]
    occurred_at: str
    observed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "custodian_refs", ref_tuple(self.custodian_refs, "custodian_refs"))
        object.__setattr__(self, "subject_refs", ref_tuple(self.subject_refs, "subject_refs"))
        object.__setattr__(
            self,
            "model_claimed_parent_refs",
            ref_tuple(self.model_claimed_parent_refs, "model_claimed_parent_refs"),
        )
        for name, enum_cls in (
            ("source_kind", SourceKind),
            ("instruction_authority", InstructionAuthority),
            ("integrity", Integrity),
            ("assertion_status", AssertionStatus),
        ):
            object.__setattr__(self, name, check_enum(getattr(self, name), enum_cls, name))
        check_utc(self.occurred_at, path="occurred_at")
        check_utc(self.observed_at, path="observed_at")

    def to_payload(self) -> dict[str, Any]:
        return {
            "producer_ref": self.producer_ref.to_payload(),
            "source_kind": self.source_kind.value,
            "origin_event_ref": self.origin_event_ref.to_payload()
            if self.origin_event_ref
            else None,
            "claimed_speaker_ref": (
                self.claimed_speaker_ref.to_payload() if self.claimed_speaker_ref else None
            ),
            "instruction_authority": self.instruction_authority.value,
            "integrity": self.integrity.value,
            "custodian_refs": [ref.to_payload() for ref in self.custodian_refs],
            "subject_refs": [ref.to_payload() for ref in self.subject_refs],
            "original_audience_ref": (
                self.original_audience_ref.to_payload() if self.original_audience_ref else None
            ),
            "assertion_status": self.assertion_status.value,
            "producer_coverage_ref": (
                self.producer_coverage_ref.to_payload() if self.producer_coverage_ref else None
            ),
            "model_claimed_parent_refs": [
                ref.to_payload() for ref in self.model_claimed_parent_refs
            ],
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_payload(cls, data: object) -> Provenance:
        if not isinstance(data, dict):
            raise ValidationError("Provenance payload must be an object", path="provenance")
        expected = {
            "producer_ref",
            "source_kind",
            "origin_event_ref",
            "claimed_speaker_ref",
            "instruction_authority",
            "integrity",
            "custodian_refs",
            "subject_refs",
            "original_audience_ref",
            "assertion_status",
            "producer_coverage_ref",
            "model_claimed_parent_refs",
            "occurred_at",
            "observed_at",
        }
        if set(data) != expected:
            raise ValidationError(
                f"Provenance fields must be exactly {sorted(expected)}", path="provenance"
            )
        return cls(
            producer_ref=RecordRef.from_payload(data["producer_ref"]),
            source_kind=check_enum(data["source_kind"], SourceKind, "provenance.source_kind"),
            origin_event_ref=RecordRef.from_payload(data["origin_event_ref"])
            if data["origin_event_ref"]
            else None,
            claimed_speaker_ref=RecordRef.from_payload(data["claimed_speaker_ref"])
            if data["claimed_speaker_ref"]
            else None,
            instruction_authority=check_enum(
                data["instruction_authority"],
                InstructionAuthority,
                "provenance.instruction_authority",
            ),
            integrity=check_enum(data["integrity"], Integrity, "provenance.integrity"),
            custodian_refs=tuple(RecordRef.from_payload(item) for item in data["custodian_refs"]),
            subject_refs=tuple(RecordRef.from_payload(item) for item in data["subject_refs"]),
            original_audience_ref=RecordRef.from_payload(data["original_audience_ref"])
            if data["original_audience_ref"]
            else None,
            assertion_status=check_enum(
                data["assertion_status"], AssertionStatus, "provenance.assertion_status"
            ),
            producer_coverage_ref=RecordRef.from_payload(data["producer_coverage_ref"])
            if data["producer_coverage_ref"]
            else None,
            model_claimed_parent_refs=tuple(
                RecordRef.from_payload(item) for item in data["model_claimed_parent_refs"]
            ),
            occurred_at=data["occurred_at"],
            observed_at=data["observed_at"],
        )


# ---------------------------------------------------------------------------
# Record base (handoff §3 common header)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Record:
    """Common header for every persisted domain record.

    Subclasses add exactly the fields their §4 table lists and override
    ``_validate`` for type-specific rules; the header is validated here.
    """

    TYPE_NAME: ClassVar[str] = "Record"

    schema_version: int = RECORD_SCHEMA_VERSION
    id: str
    revision: int
    created_at: str

    def __post_init__(self) -> None:
        if not _is_int(self.schema_version) or self.schema_version != RECORD_SCHEMA_VERSION:
            raise ValidationError(
                f"schema_version must be {RECORD_SCHEMA_VERSION}", path="schema_version"
            )
        check_id(self.id, path="id")
        check_positive_int(self.revision, path="revision")
        check_utc(self.created_at, path="created_at")
        self._normalize()
        self._validate()

    def _normalize(self) -> None:
        """Coerce valid string enum inputs to enum members so records hold
        closed-vocabulary values regardless of construction style. Invalid
        strings fall through to ``_validate`` for the proper error."""
        for name, (kind, param) in _field_plan(type(self)).items():
            value = getattr(self, name)
            if kind == _CODEC_ENUM and isinstance(value, str):
                with contextlib.suppress(ValueError):
                    object.__setattr__(self, name, param(value))
            elif kind == _CODEC_SEQ:
                item_kind, item_param = param
                if item_kind == _CODEC_ENUM:
                    converted = []
                    for item in value:
                        if isinstance(item, str):
                            try:
                                converted.append(item_param(item))
                            except ValueError:
                                converted.append(item)
                        else:
                            converted.append(item)
                    object.__setattr__(self, name, tuple(converted))

    def _validate(self) -> None:
        """Hook for type-specific validation; overridden by subclasses."""


# ---------------------------------------------------------------------------
# Records: identity, authority, event and context (handoff §4.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Principal(Record):
    TYPE_NAME: ClassVar[str] = "Principal"

    kind: PrincipalKind
    verified_bindings: tuple[RecordRef, ...]
    claimed_aliases: tuple[str, ...]

    def _validate(self) -> None:
        check_enum(self.kind, PrincipalKind, "kind")
        ref_tuple(self.verified_bindings, "verified_bindings")
        if len(self.claimed_aliases) > MAX_LIST_LEN:
            raise ValidationError(f"list exceeds bound of {MAX_LIST_LEN}", path="claimed_aliases")
        for alias in self.claimed_aliases:
            check_str(alias, "claimed_aliases")


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentIdentity(Record):
    TYPE_NAME: ClassVar[str] = "AgentIdentity"

    principal_ref: RecordRef
    preference_refs: tuple[RecordRef, ...]
    autobiography_refs: tuple[RecordRef, ...]
    commitment_refs: tuple[RecordRef, ...]
    active_interest_refs: tuple[RecordRef, ...]

    def _validate(self) -> None:
        if not isinstance(self.principal_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="principal_ref")
        ref_tuple(self.preference_refs, "preference_refs")
        ref_tuple(self.autobiography_refs, "autobiography_refs")
        ref_tuple(self.commitment_refs, "commitment_refs")
        ref_tuple(self.active_interest_refs, "active_interest_refs")


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorityBinding(Record):
    TYPE_NAME: ClassVar[str] = "AuthorityBinding"

    issuer_ref: RecordRef
    domain: AuthorityDomain
    resource_id: str
    evidence_ref: RecordRef
    delegated_from: RecordRef | None
    valid_until: str | None
    revoked_at: str | None

    def _validate(self) -> None:
        check_enum(self.domain, AuthorityDomain, "domain")
        check_id(self.resource_id, path="resource_id")
        if self.valid_until is not None:
            check_utc(self.valid_until, path="valid_until")
        if self.revoked_at is not None:
            check_utc(self.revoked_at, path="revoked_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourcePolicy(Record):
    TYPE_NAME: ClassVar[str] = "ResourcePolicy"

    resource_id: str
    data_class: str
    approval_rule: ApprovalRule
    eligible_issuer_refs: tuple[RecordRef, ...]
    commitment_refs: tuple[RecordRef, ...]
    allowed_transform_ids: tuple[str, ...]
    policy_revision: int
    status: RecordStatus

    def _validate(self) -> None:
        check_id(self.resource_id, path="resource_id")
        check_str(self.data_class, "data_class", max_chars=MAX_ID_LEN)
        check_enum(self.approval_rule, ApprovalRule, "approval_rule")
        ref_tuple(self.eligible_issuer_refs, "eligible_issuer_refs")
        ref_tuple(self.commitment_refs, "commitment_refs")
        id_tuple(self.allowed_transform_ids, "allowed_transform_ids")
        check_positive_int(self.policy_revision, path="policy_revision")
        check_enum(self.status, RecordStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class UseAuthorization(Record):
    """The tabulated ``revision`` is the common header revision (design D5)."""

    TYPE_NAME: ClassVar[str] = "UseAuthorization"

    resource_id: str
    issuer_refs: tuple[RecordRef, ...]
    binding_refs: tuple[RecordRef, ...]
    operations: tuple[ResourceOperation, ...]
    permitted_purpose_classes: tuple[str, ...]
    audience_class_bounds: tuple[str, ...]
    maximum_precision: RequestedPrecision
    allowed_transform_ids: tuple[str, ...]
    valid_until: str | None
    status: RecordStatus

    def _validate(self) -> None:
        check_id(self.resource_id, path="resource_id")
        ref_tuple(self.issuer_refs, "issuer_refs")
        ref_tuple(self.binding_refs, "binding_refs")
        enum_tuple(self.operations, ResourceOperation, "operations")
        id_tuple(self.permitted_purpose_classes, "permitted_purpose_classes")
        id_tuple(self.audience_class_bounds, "audience_class_bounds")
        check_enum(self.maximum_precision, RequestedPrecision, "maximum_precision")
        id_tuple(self.allowed_transform_ids, "allowed_transform_ids")
        if self.valid_until is not None:
            check_utc(self.valid_until, path="valid_until")
        check_enum(self.status, RecordStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityEnvelope(Record):
    TYPE_NAME: ClassVar[str] = "CapabilityEnvelope"

    actor_ref: RecordRef
    account_id: str
    allowed_destination_ids: tuple[str, ...]
    allowed_effect_kinds: tuple[EffectKind, ...]
    provisioning_binding_refs: tuple[RecordRef, ...]
    rate_budget_limits: tuple[Pair, ...]
    valid_until: str | None
    status: RecordStatus

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rate_budget_limits",
            sorted_pair_tuple(self.rate_budget_limits, "rate_budget_limits"),
        )
        super().__post_init__()

    def _validate(self) -> None:
        if not isinstance(self.actor_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="actor_ref")
        check_id(self.account_id, path="account_id")
        id_tuple(self.allowed_destination_ids, "allowed_destination_ids")
        enum_tuple(self.allowed_effect_kinds, EffectKind, "allowed_effect_kinds")
        ref_tuple(self.provisioning_binding_refs, "provisioning_binding_refs")
        if self.valid_until is not None:
            check_utc(self.valid_until, path="valid_until")
        check_enum(self.status, RecordStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class Commitment(Record):
    TYPE_NAME: ClassVar[str] = "Commitment"

    subject_refs: tuple[RecordRef, ...]
    custodian_refs: tuple[RecordRef, ...]
    resource_id: str | None
    source_scope: str | None
    strength: CommitmentStrength
    internal_use_allowed: bool
    observable_influence_allowed: bool
    expression_allowed: bool
    audience_predicate_id: str
    policy_evidence_refs: tuple[RecordRef, ...]

    def _validate(self) -> None:
        ref_tuple(self.subject_refs, "subject_refs")
        ref_tuple(self.custodian_refs, "custodian_refs")
        if (self.resource_id is None) == (self.source_scope is None):
            raise ValidationError(
                "exactly one of resource_id or source_scope must be set", path="resource_id"
            )
        if self.resource_id is not None:
            check_id(self.resource_id, path="resource_id")
        if self.source_scope is not None:
            check_id(self.source_scope, path="source_scope")
        check_enum(self.strength, CommitmentStrength, "strength")
        check_bool(self.internal_use_allowed, "internal_use_allowed")
        check_bool(self.observable_influence_allowed, "observable_influence_allowed")
        check_bool(self.expression_allowed, "expression_allowed")
        check_id(self.audience_predicate_id, path="audience_predicate_id")
        ref_tuple(self.policy_evidence_refs, "policy_evidence_refs")


@dataclass(frozen=True, slots=True, kw_only=True)
class InboundEvent(Record):
    TYPE_NAME: ClassVar[str] = "InboundEvent"

    source_event_id: str
    journal_seq: int
    kind: EventKind
    actor_ref: RecordRef
    occurred_at: str
    ingested_at: str
    source_audience_ref: RecordRef
    fragment_refs: tuple[RecordRef, ...]
    reply_target_refs: tuple[RecordRef, ...]
    target_refs: tuple[RecordRef, ...]
    observed_trigger_refs: tuple[RecordRef, ...]
    recovery: RecoveryStatus

    def _validate(self) -> None:
        check_id(self.source_event_id, path="source_event_id")
        check_non_negative_int(self.journal_seq, path="journal_seq")
        check_enum(self.kind, EventKind, "kind")
        check_utc(self.occurred_at, path="occurred_at")
        check_utc(self.ingested_at, path="ingested_at")
        ref_tuple(self.fragment_refs, "fragment_refs")
        ref_tuple(self.reply_target_refs, "reply_target_refs")
        ref_tuple(self.target_refs, "target_refs")
        ref_tuple(self.observed_trigger_refs, "observed_trigger_refs")
        check_enum(self.recovery, RecoveryStatus, "recovery")


@dataclass(frozen=True, slots=True, kw_only=True)
class AudienceSnapshot(Record):
    TYPE_NAME: ClassVar[str] = "AudienceSnapshot"

    destination_id: str
    environment_kind: EnvironmentKind
    visibility: Visibility
    known_principal_refs: tuple[RecordRef, ...]
    unknown_readers: bool
    history_visibility: HistoryVisibility
    completeness: Completeness
    disclosure_context_revision: int
    valid_until: str | None

    def _validate(self) -> None:
        check_id(self.destination_id, path="destination_id")
        check_enum(self.environment_kind, EnvironmentKind, "environment_kind")
        check_enum(self.visibility, Visibility, "visibility")
        ref_tuple(self.known_principal_refs, "known_principal_refs")
        check_bool(self.unknown_readers, "unknown_readers")
        check_enum(self.history_visibility, HistoryVisibility, "history_visibility")
        check_enum(self.completeness, Completeness, "completeness")
        check_positive_int(self.disclosure_context_revision, path="disclosure_context_revision")
        if self.valid_until is not None:
            check_utc(self.valid_until, path="valid_until")


@dataclass(frozen=True, slots=True, kw_only=True)
class ConversationEpisode(Record):
    TYPE_NAME: ClassVar[str] = "ConversationEpisode"

    container_id: str
    member_event_refs: tuple[RecordRef, ...]
    reply_edges: tuple[Pair, ...]
    topic_refs: tuple[RecordRef, ...]
    summary_ref: RecordRef | None
    episode_revision: int
    pending_intent_refs: tuple[RecordRef, ...]

    def _validate(self) -> None:
        check_id(self.container_id, path="container_id")
        ref_tuple(self.member_event_refs, "member_event_refs")
        pair_tuple(self.reply_edges, "reply_edges")
        ref_tuple(self.topic_refs, "topic_refs")
        check_positive_int(self.episode_revision, path="episode_revision")
        ref_tuple(self.pending_intent_refs, "pending_intent_refs")


@dataclass(frozen=True, slots=True, kw_only=True)
class ContentFragment(Record):
    TYPE_NAME: ClassVar[str] = "ContentFragment"

    kind: FragmentKind
    payload_ref: str
    provenance: Provenance
    commitment_refs: tuple[RecordRef, ...]
    eligibility_status: EligibilityStatus

    def _validate(self) -> None:
        check_enum(self.kind, FragmentKind, "kind")
        check_id(self.payload_ref, path="payload_ref")
        if not isinstance(self.provenance, Provenance):
            raise ValidationError("expected a Provenance value", path="provenance")
        ref_tuple(self.commitment_refs, "commitment_refs")
        check_enum(self.eligibility_status, EligibilityStatus, "eligibility_status")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProducerCoverage(Record):
    TYPE_NAME: ClassVar[str] = "ProducerCoverage"

    producer_kind: ProducerKind
    input_fragment_refs: tuple[RecordRef, ...]
    input_view_refs: tuple[RecordRef, ...]
    input_intent_refs: tuple[RecordRef, ...]
    input_policy_refs: tuple[RecordRef, ...]
    sink_ref: RecordRef | None
    call_id: str
    runtime_epoch: int

    def _validate(self) -> None:
        check_enum(self.producer_kind, ProducerKind, "producer_kind")
        ref_tuple(self.input_fragment_refs, "input_fragment_refs")
        ref_tuple(self.input_view_refs, "input_view_refs")
        ref_tuple(self.input_intent_refs, "input_intent_refs")
        ref_tuple(self.input_policy_refs, "input_policy_refs")
        check_str(self.call_id, "call_id", max_chars=MAX_ID_LEN)
        check_non_negative_int(self.runtime_epoch, path="runtime_epoch")


# ---------------------------------------------------------------------------
# Records: intent, work and memory (handoff §4.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Intent(Record):
    TYPE_NAME: ClassVar[str] = "Intent"

    creator_ref: RecordRef
    goal_fragment_ref: RecordRef
    purpose_class: str
    observed_trigger_refs: tuple[RecordRef, ...]
    endorsement_revision: int
    producer_coverage_ref: RecordRef | None
    runtime_task_root_ids: tuple[str, ...]
    parent_work_ref: RecordRef | None
    allowed_destination_refs: tuple[str, ...]
    due_at: str | None
    expires_at: str | None
    state: IntentState

    def _validate(self) -> None:
        if not isinstance(self.creator_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="creator_ref")
        if not isinstance(self.goal_fragment_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="goal_fragment_ref")
        check_id(self.purpose_class, path="purpose_class")
        ref_tuple(self.observed_trigger_refs, "observed_trigger_refs")
        check_positive_int(self.endorsement_revision, path="endorsement_revision")
        id_tuple(self.runtime_task_root_ids, "runtime_task_root_ids")
        id_tuple(self.allowed_destination_refs, "allowed_destination_refs")
        if self.due_at is not None:
            check_utc(self.due_at, path="due_at")
        if self.expires_at is not None:
            check_utc(self.expires_at, path="expires_at")
        check_enum(self.state, IntentState, "state")


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkItem(Record):
    TYPE_NAME: ClassVar[str] = "WorkItem"

    intent_ref: RecordRef
    trigger_key: str
    episode_ref: RecordRef | None
    state: WorkState
    lease_owner: str | None
    lease_until_ms: int | None
    runtime_epoch: int
    main_steps_used: int
    submissions_used: int
    terminal_outcome_ref: RecordRef | None

    def _validate(self) -> None:
        if not isinstance(self.intent_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="intent_ref")
        check_str(self.trigger_key, "trigger_key", max_chars=MAX_ID_LEN)
        check_enum(self.state, WorkState, "state")
        if self.lease_owner is not None:
            check_id(self.lease_owner, path="lease_owner")
        if self.lease_until_ms is not None:
            check_non_negative_int(self.lease_until_ms, path="lease_until_ms")
        check_non_negative_int(self.runtime_epoch, path="runtime_epoch")
        check_non_negative_int(self.main_steps_used, path="main_steps_used")
        check_non_negative_int(self.submissions_used, path="submissions_used")


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryRecord(Record):
    TYPE_NAME: ClassVar[str] = "MemoryRecord"

    assertion_fragment_ref: RecordRef
    stage: MemoryStage
    scope_kind: ScopeKind
    scope_ref: str
    custodian_refs: tuple[RecordRef, ...]
    subject_refs: tuple[RecordRef, ...]
    commitment_refs: tuple[RecordRef, ...]
    assertion_status: AssertionStatus
    supersedes_ref: RecordRef | None
    valid_until: str | None
    status: MemoryStatus

    def _validate(self) -> None:
        if not isinstance(self.assertion_fragment_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="assertion_fragment_ref")
        check_enum(self.stage, MemoryStage, "stage")
        check_enum(self.scope_kind, ScopeKind, "scope_kind")
        check_id(self.scope_ref, path="scope_ref")
        ref_tuple(self.custodian_refs, "custodian_refs")
        ref_tuple(self.subject_refs, "subject_refs")
        ref_tuple(self.commitment_refs, "commitment_refs")
        check_enum(self.assertion_status, AssertionStatus, "assertion_status")
        if self.valid_until is not None:
            check_utc(self.valid_until, path="valid_until")
        check_enum(self.status, MemoryStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class Derivation(Record):
    TYPE_NAME: ClassVar[str] = "Derivation"

    output_fragment_ref: RecordRef
    producer_coverage_ref: RecordRef
    claimed_parent_refs: tuple[RecordRef, ...]
    transform_id: str | None
    authorization_refs: tuple[RecordRef, ...]
    inherited_commitment_refs: tuple[RecordRef, ...]
    input_version_vector: tuple[RecordRef, ...]
    state: DerivationState

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_version_vector",
            check_version_vector(self.input_version_vector, "input_version_vector"),
        )
        super().__post_init__()

    def _validate(self) -> None:
        if not isinstance(self.output_fragment_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="output_fragment_ref")
        if not isinstance(self.producer_coverage_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="producer_coverage_ref")
        ref_tuple(self.claimed_parent_refs, "claimed_parent_refs")
        if self.transform_id is not None:
            check_id(self.transform_id, path="transform_id")
        ref_tuple(self.authorization_refs, "authorization_refs")
        ref_tuple(self.inherited_commitment_refs, "inherited_commitment_refs")
        check_enum(self.state, DerivationState, "state")


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryAccessRequest(Record):
    """``necessity_text`` XOR ``necessity_payload_ref`` (design D5): the
    model-facing form carries text; persistence swaps it to a payload ref."""

    TYPE_NAME: ClassVar[str] = "MemoryAccessRequest"

    request_id: str
    intent_ref: RecordRef
    resource_id: str | None
    data_class: str | None
    requested_precision: RequestedPrecision
    requested_fields: tuple[str, ...]
    transform_id: str | None
    audience_ref: RecordRef
    intended_use: IntendedUse
    necessity_text: str | None
    necessity_payload_ref: str | None
    trigger_refs: tuple[RecordRef, ...]
    runtime_origin: RuntimeOrigin | None

    def _validate(self) -> None:
        check_id(self.request_id, path="request_id")
        if not isinstance(self.intent_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="intent_ref")
        if (self.resource_id is None) == (self.data_class is None):
            raise ValidationError(
                "exactly one of resource_id or data_class must be set", path="resource_id"
            )
        if self.resource_id is not None:
            check_id(self.resource_id, path="resource_id")
        if self.data_class is not None:
            check_str(self.data_class, "data_class", max_chars=MAX_ID_LEN)
        check_enum(self.requested_precision, RequestedPrecision, "requested_precision")
        id_tuple(self.requested_fields, "requested_fields")
        if self.transform_id is not None:
            check_id(self.transform_id, path="transform_id")
        if not isinstance(self.audience_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="audience_ref")
        check_enum(self.intended_use, IntendedUse, "intended_use")
        if (self.necessity_text is None) == (self.necessity_payload_ref is None):
            raise ValidationError(
                "exactly one of necessity_text or necessity_payload_ref must be set",
                path="necessity_text",
            )
        if self.necessity_text is not None:
            check_str(self.necessity_text, "necessity_text")
        if self.necessity_payload_ref is not None:
            check_id(self.necessity_payload_ref, path="necessity_payload_ref")
        ref_tuple(self.trigger_refs, "trigger_refs")
        if self.runtime_origin is not None and not isinstance(self.runtime_origin, RuntimeOrigin):
            raise ValidationError("expected a RuntimeOrigin value", path="runtime_origin")


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryAccessResult(Record):
    """Widened per addendum D07 (which outranks the handoff table): the result
    itself exposes independent use permissions and basis references —
    references only, never duplicated payload bytes (design D4.2)."""

    TYPE_NAME: ClassVar[str] = "MemoryAccessResult"

    request_id: str
    access_status: AccessStatus
    view_kind: ViewKind
    view_ref: RecordRef | None
    reason_codes: tuple[ReasonCode, ...]
    safe_feedback_template_id: str | None
    permitted_uses: tuple[IntendedUse, ...]
    producer_coverage_ref: RecordRef | None
    transform_certificate_ref: RecordRef | None
    authorization_refs: tuple[RecordRef, ...]
    inherited_commitment_refs: tuple[RecordRef, ...]

    def _validate(self) -> None:
        check_id(self.request_id, path="request_id")
        check_enum(self.access_status, AccessStatus, "access_status")
        check_enum(self.view_kind, ViewKind, "view_kind")
        enum_tuple(self.reason_codes, ReasonCode, "reason_codes")
        if self.safe_feedback_template_id is not None:
            check_id(self.safe_feedback_template_id, path="safe_feedback_template_id")
        enum_tuple(self.permitted_uses, IntendedUse, "permitted_uses")
        ref_tuple(self.authorization_refs, "authorization_refs")
        ref_tuple(self.inherited_commitment_refs, "inherited_commitment_refs")
        if self.access_status == AccessStatus.GRANTED:
            if self.view_kind == ViewKind.NONE or self.view_ref is None:
                raise ValidationError(
                    "GRANTED requires FULL/PARTIAL/ABSTRACTED with an active view ref",
                    path="view_kind",
                )
        elif self.view_kind != ViewKind.NONE or self.view_ref is not None:
            raise ValidationError(
                "non-GRANTED results carry view_kind NONE and no view", path="view_kind"
            )
        elif self.permitted_uses or self.transform_certificate_ref is not None:
            raise ValidationError(
                "non-GRANTED results carry no permitted uses and no transform basis",
                path="permitted_uses",
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryView(Record):
    TYPE_NAME: ClassVar[str] = "MemoryView"

    resource_id: str
    returned_fragment_refs: tuple[RecordRef, ...]
    authorization_refs: tuple[RecordRef, ...]
    transform_certificate_ref: RecordRef | None
    producer_coverage_ref: RecordRef
    precision: RequestedPrecision
    inherited_commitment_refs: tuple[RecordRef, ...]
    permitted_uses: tuple[IntendedUse, ...]
    audience_bounds: tuple[str, ...]
    valid_until: str | None
    status: RecordStatus

    def _validate(self) -> None:
        check_id(self.resource_id, path="resource_id")
        ref_tuple(self.returned_fragment_refs, "returned_fragment_refs")
        ref_tuple(self.authorization_refs, "authorization_refs")
        if not isinstance(self.producer_coverage_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="producer_coverage_ref")
        check_enum(self.precision, RequestedPrecision, "precision")
        ref_tuple(self.inherited_commitment_refs, "inherited_commitment_refs")
        enum_tuple(self.permitted_uses, IntendedUse, "permitted_uses")
        id_tuple(self.audience_bounds, "audience_bounds")
        if self.valid_until is not None:
            check_utc(self.valid_until, path="valid_until")
        check_enum(self.status, RecordStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class TransformCertificate(Record):
    TYPE_NAME: ClassVar[str] = "TransformCertificate"

    registry_transform_id: str
    parameter_values: tuple[Pair, ...]
    source_coverage_ref: RecordRef
    authorizing_binding_refs: tuple[RecordRef, ...]
    authorizing_use_refs: tuple[RecordRef, ...]
    discharged_commitment_refs: tuple[RecordRef, ...]
    retained_commitment_refs: tuple[RecordRef, ...]
    output_view_ref: RecordRef

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parameter_values",
            sorted_pair_tuple(self.parameter_values, "parameter_values"),
        )
        super().__post_init__()

    def _validate(self) -> None:
        check_id(self.registry_transform_id, path="registry_transform_id")
        if not isinstance(self.source_coverage_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="source_coverage_ref")
        ref_tuple(self.authorizing_binding_refs, "authorizing_binding_refs")
        ref_tuple(self.authorizing_use_refs, "authorizing_use_refs")
        ref_tuple(self.discharged_commitment_refs, "discharged_commitment_refs")
        ref_tuple(self.retained_commitment_refs, "retained_commitment_refs")
        if not isinstance(self.output_view_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="output_view_ref")


# ---------------------------------------------------------------------------
# Records: model/context, decision and effect (handoff §4.3, §4.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ComputeSink(Record):
    TYPE_NAME: ClassVar[str] = "ComputeSink"

    kind: SinkKind
    configured_endpoint: str | None
    permitted_data_classes: tuple[str, ...]
    capability_ref: RecordRef | None
    status: SinkStatus

    def _validate(self) -> None:
        check_enum(self.kind, SinkKind, "kind")
        if self.configured_endpoint is not None:
            check_str(self.configured_endpoint, "configured_endpoint")
        id_tuple(self.permitted_data_classes, "permitted_data_classes")
        check_enum(self.status, SinkStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextSnapshot(Record):
    TYPE_NAME: ClassVar[str] = "ContextSnapshot"

    identity_ref: RecordRef
    intent_ref: RecordRef
    audience_ref: RecordRef
    episode_ref: RecordRef | None
    selected_fragment_refs: tuple[RecordRef, ...]
    active_view_refs: tuple[RecordRef, ...]
    producer_coverage_ref: RecordRef
    exposure_class: ExposureClass
    inherited_commitment_refs: tuple[RecordRef, ...]
    version_vector: tuple[RecordRef, ...]
    sink_ref: RecordRef
    effect_capable: bool
    serializer_version: str
    status: SnapshotStatus

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "version_vector", check_version_vector(self.version_vector, "version_vector")
        )
        super().__post_init__()

    def _validate(self) -> None:
        if not isinstance(self.identity_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="identity_ref")
        if not isinstance(self.intent_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="intent_ref")
        if not isinstance(self.audience_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="audience_ref")
        ref_tuple(self.selected_fragment_refs, "selected_fragment_refs")
        ref_tuple(self.active_view_refs, "active_view_refs")
        if not isinstance(self.producer_coverage_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="producer_coverage_ref")
        check_enum(self.exposure_class, ExposureClass, "exposure_class")
        ref_tuple(self.inherited_commitment_refs, "inherited_commitment_refs")
        if not isinstance(self.sink_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="sink_ref")
        check_bool(self.effect_capable, "effect_capable")
        check_str(self.serializer_version, "serializer_version", max_chars=MAX_ID_LEN)
        check_enum(self.status, SnapshotStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelCallEnvelope(Record):
    TYPE_NAME: ClassVar[str] = "ModelCallEnvelope"

    snapshot_ref: RecordRef
    purpose: EnvelopePurpose
    exact_messages_payload_ref: str
    tool_schema_digest: str
    supplied_fragment_refs: tuple[RecordRef, ...]
    supplied_view_refs: tuple[RecordRef, ...]
    sink_ref: RecordRef
    request_digest: str

    def _validate(self) -> None:
        if not isinstance(self.snapshot_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="snapshot_ref")
        check_enum(self.purpose, EnvelopePurpose, "purpose")
        check_id(self.exact_messages_payload_ref, path="exact_messages_payload_ref")
        check_sha256(self.tool_schema_digest, path="tool_schema_digest")
        ref_tuple(self.supplied_fragment_refs, "supplied_fragment_refs")
        ref_tuple(self.supplied_view_refs, "supplied_view_refs")
        if not isinstance(self.sink_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="sink_ref")
        check_sha256(self.request_digest, path="request_digest")


@dataclass(frozen=True, slots=True, kw_only=True)
class PerceptionSignals(Record):
    TYPE_NAME: ClassVar[str] = "PerceptionSignals"

    event_refs: tuple[RecordRef, ...]
    window_refs: tuple[RecordRef, ...]
    probabilities: tuple[Pair, ...]
    address_flag: bool
    reply_flag: bool
    status: PerceptionStatus
    adapter_version: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "probabilities", sorted_pair_tuple(self.probabilities, "probabilities")
        )
        super().__post_init__()

    def _validate(self) -> None:
        ref_tuple(self.event_refs, "event_refs")
        ref_tuple(self.window_refs, "window_refs")
        check_bool(self.address_flag, "address_flag")
        check_bool(self.reply_flag, "reply_flag")
        check_enum(self.status, PerceptionStatus, "status")
        check_str(self.adapter_version, "adapter_version", max_chars=MAX_ID_LEN)


@dataclass(frozen=True, slots=True, kw_only=True)
class Effect(Record):
    TYPE_NAME: ClassVar[str] = "Effect"

    logical_effect_id: str
    kind: EffectKind
    account_id: str
    destination_id: str
    target_ref: RecordRef | None
    rendered_payload_ref: str
    payload_digest: str
    capability_ref: RecordRef

    def _validate(self) -> None:
        check_id(self.logical_effect_id, path="logical_effect_id")
        check_enum(self.kind, EffectKind, "kind")
        check_id(self.account_id, path="account_id")
        check_id(self.destination_id, path="destination_id")
        if self.kind == EffectKind.ADD_REACTION and self.target_ref is None:
            raise ValidationError("ADD_REACTION requires a target ref", path="target_ref")
        check_id(self.rendered_payload_ref, path="rendered_payload_ref")
        check_sha256(self.payload_digest, path="payload_digest")
        if not isinstance(self.capability_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="capability_ref")


@dataclass(frozen=True, slots=True, kw_only=True)
class EffectPlan(Record):
    TYPE_NAME: ClassVar[str] = "EffectPlan"

    intent_ref: RecordRef
    snapshot_ref: RecordRef
    disposition: Disposition
    effects: tuple[Effect, ...]
    proposal_revision: int
    proposal_digest: str
    defer_until: str | None
    defer_expires_at: str | None
    declared_fact_refs: tuple[RecordRef, ...]
    observed_target_refs: tuple[RecordRef, ...]
    producer_coverage_ref: RecordRef

    def _validate(self) -> None:
        if not isinstance(self.intent_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="intent_ref")
        if not isinstance(self.snapshot_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="snapshot_ref")
        check_enum(self.disposition, Disposition, "disposition")
        if not isinstance(self.effects, tuple) or len(self.effects) > 1:
            raise ValidationError("the slice supports at most one effect per plan", path="effects")
        for effect in self.effects:
            if not isinstance(effect, Effect):
                raise ValidationError("expected an Effect value", path="effects")
        check_positive_int(self.proposal_revision, path="proposal_revision")
        check_sha256(self.proposal_digest, path="proposal_digest")
        if self.disposition == Disposition.ACT and len(self.effects) != 1:
            raise ValidationError("ACT carries exactly one effect", path="effects")
        if self.disposition in (Disposition.QUIET, Disposition.DEFER) and self.effects:
            raise ValidationError("QUIET and DEFER carry zero effects", path="effects")
        if self.disposition == Disposition.DEFER:
            if self.defer_until is None or self.defer_expires_at is None:
                raise ValidationError(
                    "DEFER supplies defer_until and defer_expires_at", path="defer_until"
                )
            check_utc(self.defer_until, path="defer_until")
            check_utc(self.defer_expires_at, path="defer_expires_at")
        ref_tuple(self.declared_fact_refs, "declared_fact_refs")
        ref_tuple(self.observed_target_refs, "observed_target_refs")
        if not isinstance(self.producer_coverage_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="producer_coverage_ref")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewRequirement(Record):
    TYPE_NAME: ClassVar[str] = "ReviewRequirement"

    route: ReviewRoute
    reason_codes: tuple[ReasonCode, ...]
    relevant_commitment_refs: tuple[RecordRef, ...]
    expected_coverage: tuple[ReviewCoverage, ...]
    budget: int
    sink_ref: RecordRef
    policy_version_vector: tuple[RecordRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_version_vector",
            check_version_vector(self.policy_version_vector, "policy_version_vector"),
        )
        super().__post_init__()

    def _validate(self) -> None:
        check_enum(self.route, ReviewRoute, "route")
        enum_tuple(self.reason_codes, ReasonCode, "reason_codes")
        ref_tuple(self.relevant_commitment_refs, "relevant_commitment_refs")
        enum_tuple(self.expected_coverage, ReviewCoverage, "expected_coverage")
        check_non_negative_int(self.budget, path="budget")
        if not isinstance(self.sink_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="sink_ref")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewAssessment(Record):
    """The record id embeds plan id + proposal revision (design D4.3), making
    re-acceptance idempotent and 'at most one assessment per submission'
    structural at the store layer."""

    TYPE_NAME: ClassVar[str] = "ReviewAssessment"

    result: AssessmentResult
    flags: tuple[str, ...]
    runtime_assigned_coverage: ReviewCoverage
    supplied_view_refs: tuple[RecordRef, ...]
    proposal_digest: str
    policy_version_vector: tuple[RecordRef, ...]
    snapshot_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_version_vector",
            check_version_vector(self.policy_version_vector, "policy_version_vector"),
        )
        super().__post_init__()

    def _validate(self) -> None:
        check_enum(self.result, AssessmentResult, "result")
        id_tuple(self.flags, "flags")
        check_enum(self.runtime_assigned_coverage, ReviewCoverage, "runtime_assigned_coverage")
        ref_tuple(self.supplied_view_refs, "supplied_view_refs")
        check_sha256(self.proposal_digest, path="proposal_digest")
        check_positive_int(self.snapshot_revision, path="snapshot_revision")


@dataclass(frozen=True, slots=True, kw_only=True)
class CommitDecision(Record):
    TYPE_NAME: ClassVar[str] = "CommitDecision"

    kind: DecisionKind
    reason_codes: tuple[ReasonCode, ...]
    authorization_ref: RecordRef | None
    assessment_ref: RecordRef | None

    def _validate(self) -> None:
        check_enum(self.kind, DecisionKind, "kind")
        enum_tuple(self.reason_codes, ReasonCode, "reason_codes")


@dataclass(frozen=True, slots=True, kw_only=True)
class CommitAuthorization(Record):
    TYPE_NAME: ClassVar[str] = "CommitAuthorization"

    logical_effect_id: str
    proposal_revision: int
    proposal_digest: str
    payload_digest: str
    identity_ref: RecordRef
    account_id: str
    audience_disclosure_revision: int
    target_dependency_refs: tuple[RecordRef, ...]
    authority_version_vector: tuple[RecordRef, ...]
    policy_version_vector: tuple[RecordRef, ...]
    runtime_epoch: int
    capability_ref: RecordRef
    status: AuthorizationStatus

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "authority_version_vector",
            check_version_vector(self.authority_version_vector, "authority_version_vector"),
        )
        object.__setattr__(
            self,
            "policy_version_vector",
            check_version_vector(self.policy_version_vector, "policy_version_vector"),
        )
        super().__post_init__()

    def _validate(self) -> None:
        check_id(self.logical_effect_id, path="logical_effect_id")
        check_positive_int(self.proposal_revision, path="proposal_revision")
        check_sha256(self.proposal_digest, path="proposal_digest")
        check_sha256(self.payload_digest, path="payload_digest")
        if not isinstance(self.identity_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="identity_ref")
        check_id(self.account_id, path="account_id")
        check_positive_int(self.audience_disclosure_revision, path="audience_disclosure_revision")
        ref_tuple(self.target_dependency_refs, "target_dependency_refs")
        check_non_negative_int(self.runtime_epoch, path="runtime_epoch")
        if not isinstance(self.capability_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="capability_ref")
        check_enum(self.status, AuthorizationStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class DispatchLease(Record):
    """Runtime-only record; never model-facing."""

    TYPE_NAME: ClassVar[str] = "DispatchLease"

    lease_id: str
    attempt_id: str
    authorization_ref: RecordRef
    logical_effect_id: str
    payload_digest: str
    runtime_epoch: int
    transport_generation: int
    reservation_ref: RecordRef | None
    issued_at_ms: int
    expires_at_ms: int
    status: LeaseStatus

    def _validate(self) -> None:
        check_id(self.lease_id, path="lease_id")
        check_id(self.attempt_id, path="attempt_id")
        if not isinstance(self.authorization_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="authorization_ref")
        check_id(self.logical_effect_id, path="logical_effect_id")
        check_sha256(self.payload_digest, path="payload_digest")
        check_non_negative_int(self.runtime_epoch, path="runtime_epoch")
        check_non_negative_int(self.transport_generation, path="transport_generation")
        check_non_negative_int(self.issued_at_ms, path="issued_at_ms")
        check_non_negative_int(self.expires_at_ms, path="expires_at_ms")
        if self.expires_at_ms < self.issued_at_ms:
            raise ValidationError("lease expires before it is issued", path="expires_at_ms")
        check_enum(self.status, LeaseStatus, "status")


@dataclass(frozen=True, slots=True, kw_only=True)
class DisclosureReservation(Record):
    TYPE_NAME: ClassVar[str] = "DisclosureReservation"

    reservation_id: str
    constraint_group_id: str
    subject_resource_refs: tuple[RecordRef, ...]
    audience_class: str
    transform_query_key: str
    logical_effect_id: str
    state: ReservationState

    def _validate(self) -> None:
        check_id(self.reservation_id, path="reservation_id")
        check_id(self.constraint_group_id, path="constraint_group_id")
        ref_tuple(self.subject_resource_refs, "subject_resource_refs")
        check_id(self.audience_class, path="audience_class")
        check_id(self.transform_query_key, path="transform_query_key")
        check_id(self.logical_effect_id, path="logical_effect_id")
        check_enum(self.state, ReservationState, "state")


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliveryReceipt(Record):
    TYPE_NAME: ClassVar[str] = "DeliveryReceipt"

    effect_id: str
    proposal_revision: int
    attempt_id: str
    state: DeliveryState
    fake_remote_id: str | None
    generation: int
    error_code: ReasonCode | None
    observed_at: str

    def _validate(self) -> None:
        check_id(self.effect_id, path="effect_id")
        check_positive_int(self.proposal_revision, path="proposal_revision")
        check_id(self.attempt_id, path="attempt_id")
        check_enum(self.state, DeliveryState, "state")
        if self.fake_remote_id is not None:
            check_id(self.fake_remote_id, path="fake_remote_id")
        check_non_negative_int(self.generation, path="generation")
        if self.error_code is not None:
            check_enum(self.error_code, ReasonCode, "error_code")
        check_utc(self.observed_at, path="observed_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnOutcome(Record):
    TYPE_NAME: ClassVar[str] = "TurnOutcome"

    kind: OutcomeKind
    intent_ref: RecordRef | None
    work_ref: RecordRef | None
    reason_codes: tuple[ReasonCode, ...]
    effect_refs: tuple[RecordRef, ...]

    def _validate(self) -> None:
        check_enum(self.kind, OutcomeKind, "kind")
        enum_tuple(self.reason_codes, ReasonCode, "reason_codes")
        ref_tuple(self.effect_refs, "effect_refs")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewEnvelope(Record):
    """Closed field set per handoff §4.4 — no release-ledger field. The
    reviewer's cumulative-release visibility question is tracked separately
    (beads hermes-agora-2qm); extension path is a schema_version bump."""

    TYPE_NAME: ClassVar[str] = "ReviewEnvelope"

    plan_ref: RecordRef
    proposal_digest: str
    snapshot_ref: RecordRef
    audience_ref: RecordRef
    intent_ref: RecordRef
    relevant_policy_refs: tuple[RecordRef, ...]
    relevant_commitment_refs: tuple[RecordRef, ...]
    comparison_view_refs: tuple[RecordRef, ...]
    bounded_quote_fragment_refs: tuple[RecordRef, ...]
    expected_coverage_refs: tuple[RecordRef, ...]
    sink_ref: RecordRef
    exact_messages_payload_ref: str

    def _validate(self) -> None:
        if not isinstance(self.plan_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="plan_ref")
        check_sha256(self.proposal_digest, path="proposal_digest")
        if not isinstance(self.snapshot_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="snapshot_ref")
        if not isinstance(self.audience_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="audience_ref")
        if not isinstance(self.intent_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="intent_ref")
        ref_tuple(self.relevant_policy_refs, "relevant_policy_refs")
        ref_tuple(self.relevant_commitment_refs, "relevant_commitment_refs")
        ref_tuple(self.comparison_view_refs, "comparison_view_refs")
        ref_tuple(self.bounded_quote_fragment_refs, "bounded_quote_fragment_refs")
        ref_tuple(self.expected_coverage_refs, "expected_coverage_refs")
        if not isinstance(self.sink_ref, RecordRef):
            raise ValidationError("expected a RecordRef", path="sink_ref")
        check_id(self.exact_messages_payload_ref, path="exact_messages_payload_ref")


# ---------------------------------------------------------------------------
# Generic record codec (design D1/D2)
# ---------------------------------------------------------------------------

_CODEC_PRIMITIVE = "primitive"
_CODEC_ENUM = "enum"
_CODEC_REF = "ref"
_CODEC_ORIGIN = "origin"
_CODEC_PROVENANCE = "provenance"
_CODEC_PAIR = "pair"
_CODEC_NESTED = "nested"
_CODEC_SEQ = "seq"

_VALUE_OBJECT_KINDS: dict[type, str] = {
    RecordRef: _CODEC_REF,
    RuntimeOrigin: _CODEC_ORIGIN,
    Provenance: _CODEC_PROVENANCE,
    Pair: _CODEC_PAIR,
}

_field_plans: dict[type, dict[str, tuple[str, Any]]] = {}


def _field_plan(cls: type[Record]) -> dict[str, tuple[str, Any]]:
    """Cached per-class field codec plan: field name -> (kind, parameter)."""
    plan = _field_plans.get(cls)
    if plan is not None:
        return plan
    hints = get_type_hints(cls)
    plan = {}
    for field_info in fields(cls):
        annotation = hints[field_info.name]
        plan[field_info.name] = _annotation_codec(annotation)
    _field_plans[cls] = plan
    return plan


def _annotation_codec(annotation: Any) -> tuple[str, Any]:
    args = get_args(annotation)
    if get_origin(annotation) is types.UnionType:
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) != 1:
            raise ValidationError(f"unsupported optional annotation: {annotation!r}", path="codec")
        return _annotation_codec(non_none[0])
    if get_origin(annotation) is tuple:
        item_codecs = [arg for arg in args if arg is not Ellipsis]
        if len(item_codecs) != 1:
            raise ValidationError(f"unsupported tuple annotation: {annotation!r}", path="codec")
        item_kind, item_param = _annotation_codec(item_codecs[0])
        return _CODEC_SEQ, (item_kind, item_param)
    if isinstance(annotation, type):
        if annotation in (str, int, float, bool):
            return _CODEC_PRIMITIVE, annotation
        if annotation in _VALUE_OBJECT_KINDS:
            return _VALUE_OBJECT_KINDS[annotation], None
        if issubclass(annotation, StrEnum):
            return _CODEC_ENUM, annotation
        if issubclass(annotation, Record):
            return _CODEC_NESTED, annotation
    raise ValidationError(f"unsupported field annotation: {annotation!r}", path="codec")


def _encode_value(value: Any, kind: str, param: Any) -> Any:
    if value is None:
        return None
    if kind == _CODEC_PRIMITIVE:
        return value
    if kind == _CODEC_ENUM:
        return value.value
    if kind == _CODEC_REF:
        return value.to_payload()
    if kind == _CODEC_ORIGIN:
        return value.to_payload()
    if kind == _CODEC_PROVENANCE:
        return value.to_payload()
    if kind == _CODEC_PAIR:
        return value.to_payload()
    if kind == _CODEC_NESTED:
        return record_to_payload(value)
    item_kind, item_param = param
    return [_encode_value(item, item_kind, item_param) for item in value]


def _decode_value(value: Any, kind: str, param: Any, path: str) -> Any:
    if value is None:
        return None
    if kind == _CODEC_PRIMITIVE:
        if param is float and _is_int(value):
            return float(value)
        if not isinstance(value, param):
            raise ValidationError(
                f"expected {param.__name__}, got {type(value).__name__}", path=path
            )
        return value
    if kind == _CODEC_ENUM:
        return check_enum(value, param, path)
    if kind == _CODEC_REF:
        return RecordRef.from_payload(value)
    if kind == _CODEC_ORIGIN:
        return RuntimeOrigin.from_payload(value)
    if kind == _CODEC_PROVENANCE:
        return Provenance.from_payload(value)
    if kind == _CODEC_PAIR:
        return Pair.from_payload(value)
    if kind == _CODEC_NESTED:
        if not isinstance(value, dict):
            raise ValidationError("expected an object for a nested record", path=path)
        return record_from_payload(param, value)
    if not isinstance(value, list):
        raise ValidationError("expected a list", path=path)
    if len(value) > MAX_LIST_LEN:
        raise ValidationError(f"list exceeds bound of {MAX_LIST_LEN}", path=path)
    item_kind, item_param = param
    return tuple(
        _decode_value(item, item_kind, item_param, f"{path}[{i}]") for i, item in enumerate(value)
    )


def record_to_payload(record: Record) -> dict[str, Any]:
    """Encode a record to a JSON-safe mapping (canonical-bindable)."""
    payload: dict[str, Any] = {}
    for name, (kind, param) in _field_plan(type(record)).items():
        payload[name] = _encode_value(getattr(record, name), kind, param)
    return payload


def record_from_payload(cls: type[Record], data: object) -> Record:
    """Decode a mapping into a record with an exact key-set check.

    Missing AND unknown keys fail: the closed field set is part of the
    contract (handoff §3).
    """
    if not isinstance(data, dict):
        raise ValidationError(f"{cls.TYPE_NAME} payload must be an object", path=cls.TYPE_NAME)
    plan = _field_plan(cls)
    expected = set(plan)
    provided = set(data)
    if provided != expected:
        missing = sorted(expected - provided)
        unknown = sorted(provided - expected)
        details = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise ValidationError(
            f"{cls.TYPE_NAME} fields mismatch: {'; '.join(details)}",
            path=cls.TYPE_NAME,
        )
    kwargs = {
        name: _decode_value(data[name], kind, param, f"{cls.TYPE_NAME}.{name}")
        for name, (kind, param) in plan.items()
    }
    return cls(**kwargs)


RECORD_TYPES: dict[str, type[Record]] = {
    cls.TYPE_NAME: cls
    for cls in (
        Principal,
        AgentIdentity,
        AuthorityBinding,
        ResourcePolicy,
        UseAuthorization,
        CapabilityEnvelope,
        Commitment,
        InboundEvent,
        AudienceSnapshot,
        ConversationEpisode,
        ContentFragment,
        ProducerCoverage,
        Intent,
        WorkItem,
        MemoryRecord,
        Derivation,
        MemoryAccessRequest,
        MemoryAccessResult,
        MemoryView,
        TransformCertificate,
        ComputeSink,
        ContextSnapshot,
        ModelCallEnvelope,
        PerceptionSignals,
        EffectPlan,
        Effect,
        ReviewRequirement,
        ReviewAssessment,
        CommitDecision,
        CommitAuthorization,
        DispatchLease,
        DisclosureReservation,
        DeliveryReceipt,
        TurnOutcome,
        ReviewEnvelope,
    )
}

ALL_RECORD_TYPES: tuple[type[Record], ...] = tuple(
    RECORD_TYPES[name] for name in sorted(RECORD_TYPES)
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
    elif isinstance(value, list):
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
            assertion_text=data.get("assertion_text", ""),
            source_refs=tuple(RecordRef.from_payload(item) for item in data.get("source_refs", ())),
            requested_stage=check_enum(data.get("requested_stage"), MemoryStage, "requested_stage"),
            requested_scope_kind=check_enum(
                data.get("requested_scope_kind"), ScopeKind, "requested_scope_kind"
            ),
            scope_ref=data.get("scope_ref", ""),
            existing_memory_ref=RecordRef.from_payload(data["existing_memory_ref"])
            if data.get("existing_memory_ref")
            else None,
        )
    if step_kind is MainStepKind.ADOPT_PREFERENCE:
        return ModelAdoptPreferenceStep(
            kind=step_kind, preference_statement=data.get("preference_statement", "")
        )
    if step_kind is MainStepKind.CREATE_INTENT:
        return ModelCreateIntentStep(
            kind=step_kind,
            goal_text=data.get("goal_text", ""),
            purpose_class=data.get("purpose_class", ""),
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
