"""Canonical payload binding (handoff §3)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agora.contracts.errors import CanonicalizationError, ValidationError
from agora.contracts.values import RecordRef

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
