"""Generic record codec with exact key-set checks (design D1/D2)."""

from __future__ import annotations

import types
from dataclasses import fields
from enum import StrEnum
from typing import Any, get_args, get_origin, get_type_hints

from agora.contracts.errors import ValidationError
from agora.contracts.records import Record
from agora.contracts.values import (
    MAX_LIST_LEN,
    Pair,
    Provenance,
    RecordRef,
    RuntimeOrigin,
    _is_int,
    check_enum,
)

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
