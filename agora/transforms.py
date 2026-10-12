"""Fixed transform registry (handoff §4.2; design D3).

Exactly two registry ids exist. Both are pure functions over an authorized
view payload plus fixture-fixed parameters supplied by the run manifest —
no arbitrary functions, no threshold search, no dynamic registry edits. A
transform's output view carries its own use permissions (addendum: an
affordability predicate may itself reveal finances).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agora.contracts import Commitment, Pair, RequestedPrecision, ValidationError

FIXED_FIELD_VIEW = "FIXED_FIELD_VIEW"
FIXED_AFFORDABILITY_PREDICATE = "FIXED_AFFORDABILITY_PREDICATE"

#: Discharge rules for transform certificates. A certificate discharges
#: only what its rule explicitly names; every other inherited commitment
#: survives into the output view.
#:
#: - NONE: the output still carries the protected content (field values);
#:   nothing is discharged.
#: - INTERNAL_FLOW_ONLY: the output provably carries no protected value
#:   (a bare predicate) and the output view's permitted uses are internal
#:   only, so commitments that restrict only outward flow — internal use
#:   allowed, observable influence and expression forbidden — no longer
#:   bear on the output. Commitments that also restrict internal use or
#:   that permit expression are retained (an affordability predicate may
#:   itself reveal finances; addendum).
DISCHARGE_NONE = "NONE"
DISCHARGE_INTERNAL_FLOW_ONLY = "INTERNAL_FLOW_ONLY"

TransformFunction = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class TransformSpec:
    transform_id: str
    description: str
    parameter_names: tuple[str, ...]
    discharge_rule: str
    output_precision: RequestedPrecision
    run: TransformFunction


def _field_view(payload: Mapping[str, Any], parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Project the fixture-selected fields out of the authorized payload.

    The ``fields`` parameter is a comma-separated field-name list fixed by
    the manifest; absent payload fields are omitted, never invented.
    """
    selected = [name.strip() for name in str(parameters["fields"]).split(",") if name.strip()]
    return {"fields": {name: payload[name] for name in selected if name in payload}}


def _affordability_predicate(
    payload: Mapping[str, Any], parameters: Mapping[str, Any]
) -> dict[str, Any]:
    """Fixture-fixed price against the protected ``available`` amount: the
    output exposes the predicate result only, never the amounts."""
    price = float(parameters["price"])
    available = float(payload["available"])
    return {"affordable": available >= price}


TRANSFORM_REGISTRY: dict[str, TransformSpec] = {
    FIXED_FIELD_VIEW: TransformSpec(
        transform_id=FIXED_FIELD_VIEW,
        description="Project fixture-selected fields from an authorized view payload.",
        parameter_names=("fields",),
        discharge_rule=DISCHARGE_NONE,
        output_precision=RequestedPrecision.FIELDS,
        run=_field_view,
    ),
    FIXED_AFFORDABILITY_PREDICATE: TransformSpec(
        transform_id=FIXED_AFFORDABILITY_PREDICATE,
        description="Boolean affordability of a fixture-fixed price against the protected"
        " 'available' amount; exposes the predicate result only.",
        parameter_names=("price",),
        discharge_rule=DISCHARGE_INTERNAL_FLOW_ONLY,
        output_precision=RequestedPrecision.ABSTRACT,
        run=_affordability_predicate,
    ),
}


def discharged_by(spec: TransformSpec, commitment: Commitment) -> bool:
    """Apply the spec's discharge rule to one inherited commitment."""
    if spec.discharge_rule == DISCHARGE_NONE:
        return False
    return (
        commitment.internal_use_allowed
        and not commitment.observable_influence_allowed
        and not commitment.expression_allowed
    )


def transform_spec(transform_id: str) -> TransformSpec:
    spec = TRANSFORM_REGISTRY.get(transform_id)
    if spec is None:
        raise ValidationError(
            f"transform id {transform_id!r} is not in the fixed registry"
            f" ({', '.join(sorted(TRANSFORM_REGISTRY))})",
            path="transform_id",
        )
    return spec


def validate_transform_parameters(transform_id: str, parameters: tuple[Pair, ...]) -> None:
    """Fixture parameters must name exactly the spec's parameter set — no
    extra dials, no missing ones."""
    spec = transform_spec(transform_id)
    provided = {pair.first for pair in parameters}
    expected = set(spec.parameter_names)
    if provided != expected:
        raise ValidationError(
            f"transform {transform_id} parameters must be exactly"
            f" {sorted(expected)}, got {sorted(provided)}",
            path="transform_parameters",
        )
