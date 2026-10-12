"""Shared M2 fixture payloads: the five handoff resource classes.

Handoff §10 story 3: seed a public project fact, an agent-private ordinary
preference, a human financial protected fact, a third-party confidential
disclosure, and a joint project protected record — every record carrying
explicit fixture scope/commitments, never a name-based classifier. These
builders feed the manifest registries; later M2 tasks (evaluation, routing,
stories) reuse them.
"""

from __future__ import annotations

from typing import Any

ANCHOR = "2026-10-12T12:00:00Z"

HUMAN = "principal:human"
THIRD_PARTY = "principal:third"
OPERATOR = "principal:operator"
AGENT = "principal:agent"

FINANCE_RESOURCE = "fragment:res:finance"
THIRD_PARTY_RESOURCE = "fragment:res:third-party-disclosure"
JOINT_RESOURCE = "fragment:res:joint-project"
PROJECT_FACT_RESOURCE = "fragment:res:project-fact"
AGENT_PREF_RESOURCE = "fragment:res:agent-preference"


def five_class_registries() -> dict[str, Any]:
    """The five handoff resource classes as manifest registries section.

    - public project fact: ordinary, no commitments, room-scoped public.
    - agent-private ordinary preference: the agent's own adopted statement,
      ordinary discretion only.
    - human financial protected fact: STRONG no-expression commitment,
      SINGLE_ISSUER policy, agent authorized for FIELDS reads under a
      purpose class; affordability transform allowed.
    - third-party confidential disclosure: STRONG, binding exists but the
      agent holds NO use authorization (the denial case).
    - joint project protected record: ALL_LISTED policy over two issuers,
      authorization carrying both issuers' bindings (the joint-grant case).
    """
    return {
        "principals": [
            {"id": HUMAN, "kind": "HUMAN_OPERATOR", "aliases": ["morgan"]},
            {"id": THIRD_PARTY, "kind": "THIRD_PARTY", "aliases": ["vesper"]},
            {"id": OPERATOR, "kind": "HUMAN_OPERATOR", "aliases": ["ops"]},
        ],
        "resources": [
            {  # 1. public project fact
                "id": PROJECT_FACT_RESOURCE,
                "text": "The agora replay slice ships a deterministic observer trace.",
                "producer": HUMAN,
                "source_kind": "SOCIAL_MESSAGE",
                "assertion_status": "OBSERVED_STATEMENT",
            },
            {  # 2. agent-private ordinary preference
                "id": AGENT_PREF_RESOURCE,
                "text": "Prefers concrete examples over abstract summaries.",
                "producer": AGENT,
                "source_kind": "AGENT_OUTPUT",
                "assertion_status": "AGENT_ADOPTED_PREFERENCE",
                "commitments": ["commit:agent-pref-ordinary"],
            },
            {  # 3. human financial protected fact
                "id": FINANCE_RESOURCE,
                "text": "monthly budget leaves 250 available after commitments",
                "producer": HUMAN,
                "source_kind": "SOCIAL_MESSAGE",
                "assertion_status": "ATTRIBUTED_ASSERTION",
                "commitments": ["commit:finance-strong"],
            },
            {  # 4. third-party confidential disclosure
                "id": THIRD_PARTY_RESOURCE,
                "text": "vesper is changing jobs next month; told in confidence",
                "producer": THIRD_PARTY,
                "source_kind": "SOCIAL_MESSAGE",
                "assertion_status": "ATTRIBUTED_ASSERTION",
                "commitments": ["commit:third-party-strong"],
            },
            {  # 5. joint project protected record
                "id": JOINT_RESOURCE,
                "text": "joint roadmap decision pending; both custodians gated",
                "producer": HUMAN,
                "source_kind": "SOCIAL_MESSAGE",
                "assertion_status": "ATTRIBUTED_ASSERTION",
                "commitments": ["commit:joint-strong"],
            },
            {  # evidence fragment backing the authority bindings
                "id": "fragment:evidence:bindings",
                "text": "observed authority grants for the fixture",
                "producer": OPERATOR,
                "source_kind": "FIXTURE_CONTROL",
                "assertion_status": "OBSERVED_STATEMENT",
            },
        ],
        "commitments": [
            {
                "id": "commit:agent-pref-ordinary",
                "subjects": [AGENT],
                "custodians": [AGENT],
                "resource_id": AGENT_PREF_RESOURCE,
                "strength": "ORDINARY_DISCRETION",
                "internal_use_allowed": True,
                "observable_influence_allowed": True,
                "expression_allowed": True,
                "audience_predicate_id": "pred:public-rooms",
            },
            {
                "id": "commit:finance-strong",
                "subjects": [HUMAN],
                "custodians": [HUMAN],
                "resource_id": FINANCE_RESOURCE,
                "strength": "STRONG",
                "internal_use_allowed": True,
                "observable_influence_allowed": False,
                "expression_allowed": False,
                "audience_predicate_id": "pred:subject-only",
                "policy_evidence": ["policy:finance"],
            },
            {
                "id": "commit:third-party-strong",
                "subjects": [THIRD_PARTY],
                "custodians": [THIRD_PARTY],
                "resource_id": THIRD_PARTY_RESOURCE,
                "strength": "STRONG",
                "internal_use_allowed": False,
                "observable_influence_allowed": False,
                "expression_allowed": False,
                "audience_predicate_id": "pred:subject-only",
                "policy_evidence": ["policy:third-party"],
            },
            {
                "id": "commit:joint-strong",
                "subjects": [HUMAN, THIRD_PARTY],
                "custodians": [HUMAN, THIRD_PARTY],
                "resource_id": JOINT_RESOURCE,
                "strength": "STRONG",
                "internal_use_allowed": True,
                "observable_influence_allowed": False,
                "expression_allowed": True,
                "audience_predicate_id": "pred:joint-custodians",
                "policy_evidence": ["policy:joint"],
            },
        ],
        "policies": [
            {
                "id": "policy:finance",
                "resource_id": FINANCE_RESOURCE,
                "data_class": "financial",
                "approval_rule": "SINGLE_ISSUER",
                "eligible_issuers": [HUMAN],
                "commitments": ["commit:finance-strong"],
                "allowed_transforms": ["FIXED_AFFORDABILITY_PREDICATE"],
            },
            {
                "id": "policy:third-party",
                "resource_id": THIRD_PARTY_RESOURCE,
                "data_class": "third-party-confidential",
                "approval_rule": "SINGLE_ISSUER",
                "eligible_issuers": [THIRD_PARTY],
                "commitments": ["commit:third-party-strong"],
            },
            {
                "id": "policy:joint",
                "resource_id": JOINT_RESOURCE,
                "data_class": "joint-project",
                "approval_rule": "ALL_LISTED",
                "eligible_issuers": [HUMAN, THIRD_PARTY],
                "commitments": ["commit:joint-strong"],
            },
        ],
        "bindings": [
            {
                "id": "binding:human-finance",
                "issuer": HUMAN,
                "domain": "RESOURCE_USE",
                "resource_id": FINANCE_RESOURCE,
                "evidence": "fragment:evidence:bindings",
            },
            {
                "id": "binding:third-party-disclosure",
                "issuer": THIRD_PARTY,
                "domain": "RESOURCE_USE",
                "resource_id": THIRD_PARTY_RESOURCE,
                "evidence": "fragment:evidence:bindings",
            },
            {
                "id": "binding:human-joint",
                "issuer": HUMAN,
                "domain": "RESOURCE_USE",
                "resource_id": JOINT_RESOURCE,
                "evidence": "fragment:evidence:bindings",
            },
            {
                "id": "binding:third-joint",
                "issuer": THIRD_PARTY,
                "domain": "RESOURCE_USE",
                "resource_id": JOINT_RESOURCE,
                "evidence": "fragment:evidence:bindings",
            },
            {
                "id": "binding:human-joint-disclosure",
                "issuer": HUMAN,
                "domain": "DISCLOSURE",
                "resource_id": JOINT_RESOURCE,
                "evidence": "fragment:evidence:bindings",
            },
            {
                "id": "binding:third-joint-disclosure",
                "issuer": THIRD_PARTY,
                "domain": "DISCLOSURE",
                "resource_id": JOINT_RESOURCE,
                "evidence": "fragment:evidence:bindings",
            },
        ],
        "authorizations": [
            {
                "id": "use:finance-fields",
                "resource_id": FINANCE_RESOURCE,
                "issuers": [HUMAN],
                "bindings": ["binding:human-finance"],
                "operations": ["READ", "TRANSFORM"],
                "purpose_classes": ["social-participation"],
                "audience_bounds": ["room:public"],
                "precision": "FIELDS",
                "transforms": ["FIXED_AFFORDABILITY_PREDICATE"],
            },
            {
                "id": "use:joint-expression",
                "resource_id": JOINT_RESOURCE,
                "issuers": [HUMAN, THIRD_PARTY],
                "bindings": [
                    "binding:human-joint",
                    "binding:third-joint",
                    "binding:human-joint-disclosure",
                    "binding:third-joint-disclosure",
                ],
                "operations": ["READ", "EXPRESS"],
                "purpose_classes": ["project-coordination"],
                "audience_bounds": ["room:public"],
                "precision": "ABSTRACT",
            },
        ],
        "transforms": [
            {"id": "FIXED_AFFORDABILITY_PREDICATE", "parameters": [["price", 30]]},
        ],
    }


def manifest_with_registries(
    registries: dict[str, Any], *, fixture_id: str = "m2-registries"
) -> dict[str, Any]:
    """A minimal bootable manifest carrying the registries section."""
    return {
        "manifest_version": 2,
        "fixture_id": fixture_id,
        "clock_anchor": ANCHOR,
        "destination_id": "room:public",
        "allowed_destinations": ["room:public"],
        "events": [
            {
                "at_ms": 1000,
                "event_id": "evt:boot",
                "kind": "MESSAGE_CREATED",
                "text": "hello hermes",
            }
        ],
        "steps": [
            {"kind": "FINISH", "outcome": "NO_ACTION"},
        ],
        "registries": registries,
    }
