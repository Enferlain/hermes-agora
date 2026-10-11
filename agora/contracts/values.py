"""Constants, shared validators, and value types (handoff §3)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from agora.contracts.enums import AssertionStatus, InstructionAuthority, Integrity, SourceKind
from agora.contracts.errors import ValidationError

E = TypeVar("E", bound=StrEnum)

RECORD_SCHEMA_VERSION = 2

MAX_ID_LEN = 128
MAX_LIST_LEN = 64
MAX_TEXT_CHARS = 2000
MAX_PAYLOAD_BYTES = 65536

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}$")
RFC3339_UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
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
