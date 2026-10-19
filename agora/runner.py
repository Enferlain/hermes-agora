"""Replay runner: fixture manifest, serialized event/turn loop, trace output.

The manifest is fixture data through the authenticated control path; its
scripted steps are raw payloads parsed through the model boundary
(``parse_model_step``), so runtime-only field discipline applies to
fixtures exactly as it would to a model adapter. The runner derives no
domain policy from filenames or text (handoff §2).

Since manifest version 2 the manifest carries a ``registries`` section —
principals, protected/ordinary resources, commitments, policies, authority
bindings, use authorizations, and fixed transform parameters — seeded
through this trusted port at bootstrap (design D7). Unknown registry ids
fail startup.
"""

from __future__ import annotations

import json
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from agora.commit_gate import CommitGate
from agora.context_memory import ContextMemory
from agora.contracts import (
    AgentIdentity,
    ApprovalRule,
    AssertionStatus,
    AudienceSnapshot,
    AuthorityBinding,
    AuthorityDomain,
    CapabilityEnvelope,
    Commitment,
    CommitmentStrength,
    ContentFragment,
    EligibilityStatus,
    FragmentKind,
    InstructionAuthority,
    Integrity,
    IntentState,
    Pair,
    Principal,
    PrincipalKind,
    Provenance,
    RecordRef,
    RecordStatus,
    RequestedPrecision,
    ResourceOperation,
    ResourcePolicy,
    SourceKind,
    UseAuthorization,
    ValidationError,
    canonical_json_bytes,
    check_id,
    parse_model_step,
)
from agora.coordinator import MECHANICAL_PURPOSE_CLASS, Coordinator, TurnConfig
from agora.fake_sidecar import FakeSidecar, FakeWorld
from agora.ports import (
    CrashCheckpoint,
    FaultInjector,
    InjectedCrash,
    ScriptedMainModel,
    VirtualClock,
)
from agora.stores import CoreStore, JournalEntry, TransportStore
from agora.transforms import transform_spec, validate_transform_parameters

MANIFEST_VERSION = 2


class ManifestError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class FixtureEvent:
    """One scripted journal event. Reusing ``event_id`` with a higher
    ``source_revision`` (kind MESSAGE_UPDATED) models an upstream edit of
    the same message; MESSAGE_DELETED carries no fragments."""

    at_ms: int
    event_id: str
    kind: str
    text: str
    source_revision: int = 1


def _parse_event(data: object) -> FixtureEvent:
    if not isinstance(data, dict) or not {"at_ms", "event_id", "kind", "text"} <= set(data) <= {
        "at_ms",
        "event_id",
        "kind",
        "text",
        "source_revision",
    }:
        raise ManifestError(
            "each event must be exactly {at_ms, event_id, kind, text[, source_revision]}"
        )
    try:
        at_ms = int(data["at_ms"])
        source_revision = int(data.get("source_revision", 1))
    except (TypeError, ValueError) as error:
        raise ManifestError(f"event numeric field invalid: {error}") from error
    if source_revision < 1:
        raise ManifestError(f"source_revision must be >= 1, got {source_revision}")
    return FixtureEvent(
        at_ms=at_ms,
        event_id=str(data["event_id"]),
        kind=str(data["kind"]),
        text=str(data["text"]),
        source_revision=source_revision,
    )


def _validate_event_sequence(events: tuple[FixtureEvent, ...]) -> None:
    """Edit sequences must be coherent: an id's first journal entry is its
    revision 1, later entries strictly increase, and no (id, revision)
    pair repeats — malformed manifests fail at parse, never as raw store
    errors mid-run."""
    seen_revisions: dict[str, int] = {}
    pairs: set[tuple[str, int]] = set()
    for event in sorted(events, key=lambda e: e.at_ms):
        if (event.event_id, event.source_revision) in pairs:
            raise ManifestError(
                f"event {event.event_id!r} repeats source_revision {event.source_revision}"
            )
        pairs.add((event.event_id, event.source_revision))
        previous = seen_revisions.get(event.event_id)
        if previous is None:
            if event.source_revision != 1:
                raise ManifestError(
                    f"event {event.event_id!r} first appears at source_revision"
                    f" {event.source_revision}; revision 1 must come first"
                )
        elif event.source_revision <= previous:
            raise ManifestError(
                f"event {event.event_id!r} revisions must strictly increase"
                f" ({previous} then {event.source_revision})"
            )
        seen_revisions[event.event_id] = event.source_revision


@dataclass(frozen=True, slots=True)
class FaultPlanStep:
    """One scripted fault in the manifest's plan (M3 D6): a named crash
    checkpoint fired while processing ``after_event``, then a restart of
    the named domain (``core`` reopens core.sqlite — the runtime epoch
    advances and RUNNING work returns READY; ``transport`` reopens
    transport.sqlite — the sidecar generation advances; ``none`` keeps
    running). The fault plan is a first-class fixture input, so a
    manifest with faults replays as deterministically as one without."""

    after_event: str
    checkpoint: str
    restart: str = "core"


# ---------------------------------------------------------------------------
# Registry seeds (manifest version 2, design D7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrincipalSeed:
    id: str
    kind: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResourceSeed:
    id: str
    text: str
    producer: str
    source_kind: str
    assertion_status: str
    commitments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommitmentSeed:
    id: str
    subjects: tuple[str, ...]
    custodians: tuple[str, ...]
    resource_id: str | None
    source_scope: str | None
    strength: str
    internal_use_allowed: bool
    observable_influence_allowed: bool
    expression_allowed: bool
    audience_predicate_id: str
    policy_evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PolicySeed:
    id: str
    resource_id: str
    data_class: str
    approval_rule: str
    eligible_issuers: tuple[str, ...]
    commitments: tuple[str, ...]
    allowed_transforms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BindingSeed:
    id: str
    issuer: str
    domain: str
    resource_id: str
    evidence: str
    delegated_from: str | None
    valid_until: str | None


@dataclass(frozen=True, slots=True)
class AuthorizationSeed:
    id: str
    resource_id: str
    issuers: tuple[str, ...]
    bindings: tuple[str, ...]
    operations: tuple[str, ...]
    purpose_classes: tuple[str, ...]
    audience_bounds: tuple[str, ...]
    precision: str
    transforms: tuple[str, ...]
    valid_until: str | None


@dataclass(frozen=True, slots=True)
class TransformSeed:
    id: str
    parameters: tuple[Pair, ...]


@dataclass(frozen=True, slots=True)
class Registries:
    principals: tuple[PrincipalSeed, ...] = ()
    resources: tuple[ResourceSeed, ...] = ()
    commitments: tuple[CommitmentSeed, ...] = ()
    policies: tuple[PolicySeed, ...] = ()
    bindings: tuple[BindingSeed, ...] = ()
    authorizations: tuple[AuthorizationSeed, ...] = ()
    transforms: tuple[TransformSeed, ...] = ()
    #: The registered purpose-class vocabulary (handoff §4.4): the bounded
    #: strings a model CREATE_INTENT may pick from — matched against
    #: permissions, never evidence for issuing one. Distinct from each
    #: authorization's own purpose_classes matching list (bead njy).
    purpose_classes: tuple[str, ...] = ()


def _require_fields(
    data: object, required: set[str], optional: AbstractSet[str], label: str
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ManifestError(f"{label} must be an object")
    provided = set(data)
    if not required <= provided or not provided <= required | optional:
        missing = sorted(required - provided)
        unknown = sorted(provided - required - optional)
        details = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise ManifestError(f"{label} fields: {'; '.join(details)}")
    return data


def _str_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManifestError(f"{label} must be a list of strings")
    return tuple(value)


def _enum[E: StrEnum](value: str, enum_cls: type[E], label: str) -> E:
    """Typed coercion of a manifest vocabulary string; unknown values fail
    with the manifest's own error, not a bare ValueError."""
    try:
        return enum_cls(value)
    except ValueError:
        members = ", ".join(member.value for member in enum_cls)
        raise ManifestError(f"{label} must be one of {members}; got {value!r}") from None


def _section(
    data: dict[str, Any], name: str, required: set[str], optional: AbstractSet[str] = frozenset()
) -> list[dict[str, Any]]:
    entries = data.get(name, [])
    if not isinstance(entries, list):
        raise ManifestError(f"registries.{name} must be a list")
    label = f"registries.{name} entry"
    return [_require_fields(entry, required, optional, label) for entry in entries]


def _require_parameters(parameters: object) -> bool:
    if not isinstance(parameters, list) or not all(
        isinstance(pair, list) and len(pair) == 2 for pair in parameters
    ):
        raise ManifestError("registries.transforms parameters must be [name, value] pairs")
    return True


def _parse_registries(data: object) -> Registries:
    if not isinstance(data, dict):
        raise ManifestError("registries must be an object")
    sections = {
        "principals",
        "resources",
        "commitments",
        "policies",
        "bindings",
        "authorizations",
        "transforms",
        "purpose_classes",
    }
    if not set(data) <= sections:
        raise ManifestError(f"registries sections must be within {sorted(sections)}")
    principals = tuple(
        PrincipalSeed(
            id=entry["id"],
            kind=entry["kind"],
            aliases=_str_tuple(entry.get("aliases", []), "registries.principals aliases"),
        )
        for entry in _section(data, "principals", {"id", "kind"}, {"aliases"})
    )
    resources = tuple(
        ResourceSeed(
            id=entry["id"],
            text=entry["text"],
            producer=entry["producer"],
            source_kind=entry["source_kind"],
            assertion_status=entry["assertion_status"],
            commitments=_str_tuple(
                entry.get("commitments", []), "registries.resources commitments"
            ),
        )
        for entry in _section(
            data,
            "resources",
            {"id", "text", "producer", "source_kind", "assertion_status"},
            {"commitments"},
        )
    )
    commitments = tuple(
        CommitmentSeed(
            id=entry["id"],
            subjects=_str_tuple(entry["subjects"], "registries.commitments subjects"),
            custodians=_str_tuple(entry["custodians"], "registries.commitments custodians"),
            resource_id=entry.get("resource_id"),
            source_scope=entry.get("source_scope"),
            strength=entry["strength"],
            internal_use_allowed=entry["internal_use_allowed"],
            observable_influence_allowed=entry["observable_influence_allowed"],
            expression_allowed=entry["expression_allowed"],
            audience_predicate_id=entry["audience_predicate_id"],
            policy_evidence=_str_tuple(
                entry.get("policy_evidence", []), "registries.commitments policy_evidence"
            ),
        )
        for entry in _section(
            data,
            "commitments",
            {
                "id",
                "subjects",
                "custodians",
                "strength",
                "internal_use_allowed",
                "observable_influence_allowed",
                "expression_allowed",
                "audience_predicate_id",
            },
            {"resource_id", "source_scope", "policy_evidence"},
        )
    )
    policies = tuple(
        PolicySeed(
            id=entry["id"],
            resource_id=entry["resource_id"],
            data_class=entry["data_class"],
            approval_rule=entry["approval_rule"],
            eligible_issuers=_str_tuple(
                entry["eligible_issuers"], "registries.policies eligible_issuers"
            ),
            commitments=_str_tuple(entry.get("commitments", []), "registries.policies commitments"),
            allowed_transforms=_str_tuple(
                entry.get("allowed_transforms", []), "registries.policies allowed_transforms"
            ),
        )
        for entry in _section(
            data,
            "policies",
            {"id", "resource_id", "data_class", "approval_rule", "eligible_issuers"},
            {"commitments", "allowed_transforms"},
        )
    )
    bindings = tuple(
        BindingSeed(
            id=entry["id"],
            issuer=entry["issuer"],
            domain=entry["domain"],
            resource_id=entry["resource_id"],
            evidence=entry["evidence"],
            delegated_from=entry.get("delegated_from"),
            valid_until=entry.get("valid_until"),
        )
        for entry in _section(
            data,
            "bindings",
            {"id", "issuer", "domain", "resource_id", "evidence"},
            {"delegated_from", "valid_until"},
        )
    )
    authorizations = tuple(
        AuthorizationSeed(
            id=entry["id"],
            resource_id=entry["resource_id"],
            issuers=_str_tuple(entry["issuers"], "registries.authorizations issuers"),
            bindings=_str_tuple(entry["bindings"], "registries.authorizations bindings"),
            operations=_str_tuple(entry["operations"], "registries.authorizations operations"),
            purpose_classes=_str_tuple(
                entry["purpose_classes"], "registries.authorizations purpose_classes"
            ),
            audience_bounds=_str_tuple(
                entry["audience_bounds"], "registries.authorizations audience_bounds"
            ),
            precision=entry["precision"],
            transforms=_str_tuple(
                entry.get("transforms", []), "registries.authorizations transforms"
            ),
            valid_until=entry.get("valid_until"),
        )
        for entry in _section(
            data,
            "authorizations",
            {
                "id",
                "resource_id",
                "issuers",
                "bindings",
                "operations",
                "purpose_classes",
                "audience_bounds",
                "precision",
            },
            {"transforms", "valid_until"},
        )
    )
    transforms = tuple(
        TransformSeed(
            id=entry["id"],
            parameters=tuple(Pair(first=pair[0], second=pair[1]) for pair in entry["parameters"]),
        )
        for entry in _section(data, "transforms", {"id", "parameters"})
        if _require_parameters(entry["parameters"])
    )
    for seed in transforms:
        validate_transform_parameters(seed.id, seed.parameters)
    purpose_classes = (
        _str_tuple(data["purpose_classes"], "registries.purpose_classes")
        if "purpose_classes" in data
        else ()
    )
    for name in purpose_classes:
        # Every other manifest vocabulary fails at parse or seed time; a
        # typo'd purpose class must too, not surface later as a mid-run
        # POLICY_FORBIDDEN hold (registry entries must be claimable).
        try:
            check_id(name, path=f"registries.purpose_classes entry {name!r}")
        except ValidationError as error:
            raise ManifestError(str(error)) from error
    return Registries(
        principals=principals,
        resources=resources,
        commitments=commitments,
        policies=policies,
        bindings=bindings,
        authorizations=authorizations,
        transforms=transforms,
        purpose_classes=purpose_classes,
    )


@dataclass(frozen=True, slots=True)
class RunManifest:
    fixture_id: str
    clock_anchor: str
    destination_id: str
    allowed_destinations: tuple[str, ...]
    events: tuple[FixtureEvent, ...]
    steps: tuple[Any, ...]  # parsed MainStep objects, consumed in order
    registries: Registries = Registries()
    faults: tuple[FaultPlanStep, ...] = ()

    @classmethod
    def from_payload(cls, data: object) -> RunManifest:
        if not isinstance(data, dict):
            raise ManifestError("manifest must be an object")
        required = {
            "manifest_version",
            "fixture_id",
            "clock_anchor",
            "destination_id",
            "allowed_destinations",
            "events",
            "steps",
        }
        provided = set(data)
        if not required <= provided or not provided <= required | {"registries", "faults"}:
            raise ManifestError(
                f"manifest fields must be within {sorted(required | {'registries', 'faults'})}"
                f" including {sorted(required)}; got {sorted(provided)}"
            )
        version = data["manifest_version"]
        if version not in (1, MANIFEST_VERSION):
            raise ManifestError(f"unsupported manifest version {version!r}")
        if version < MANIFEST_VERSION and "registries" in data:
            raise ManifestError("registries require manifest_version 2")
        events = tuple(_parse_event(event) for event in data["events"])
        _validate_event_sequence(events)
        steps = tuple(parse_model_step(step) for step in data["steps"])
        registries = _parse_registries(data["registries"]) if "registries" in data else Registries()
        faults = cls._parse_faults(data.get("faults"), events)
        return cls(
            fixture_id=str(data["fixture_id"]),
            clock_anchor=str(data["clock_anchor"]),
            destination_id=str(data["destination_id"]),
            allowed_destinations=tuple(str(item) for item in data["allowed_destinations"]),
            events=events,
            steps=steps,
            registries=registries,
            faults=faults,
        )

    @staticmethod
    def _parse_faults(data: object, events: tuple[FixtureEvent, ...]) -> tuple[FaultPlanStep, ...]:
        if data is None:
            return ()
        if not isinstance(data, list):
            raise ManifestError("faults must be a list")
        known_events = {event.event_id for event in events}
        steps: list[FaultPlanStep] = []
        for item in data:
            if not isinstance(item, dict) or not {"after_event", "checkpoint"} <= set(item) <= {
                "after_event",
                "checkpoint",
                "restart",
            }:
                raise ManifestError(
                    "each fault must be exactly {after_event, checkpoint[, restart]}"
                )
            checkpoint = str(item["checkpoint"])
            if checkpoint not in {member.value for member in CrashCheckpoint}:
                raise ManifestError(f"unknown crash checkpoint {checkpoint!r}")
            restart = str(item.get("restart", "core"))
            if restart not in ("core", "transport", "none"):
                raise ManifestError(f"fault restart must be core/transport/none, got {restart!r}")
            after_event = str(item["after_event"])
            if after_event not in known_events:
                raise ManifestError(f"fault cites unknown event id {after_event!r}")
            if checkpoint != "AFTER_INGEST_BEFORE_ACK" and restart != "core":
                # only a core reopen performs the RUNNING->READY recovery a
                # mid-turn crash needs; any other restart strands the work
                raise ManifestError(
                    f"fault on {checkpoint!r} requires restart 'core' (got {restart!r})"
                )
            steps.append(
                FaultPlanStep(after_event=after_event, checkpoint=checkpoint, restart=restart)
            )
        keys = [(step.after_event, step.checkpoint) for step in steps]
        if len(keys) != len(set(keys)):
            raise ManifestError("fault plan reuses the same (after_event, checkpoint)")
        return tuple(steps)


# ---------------------------------------------------------------------------
# Registry seeding: the trusted fixture port (design D7)
# ---------------------------------------------------------------------------


def seed_registries(core: CoreStore, clock: VirtualClock, registries: Registries) -> None:
    """Seed the fixture registries into core.records in one batch.

    Cross-references between entries are resolved first — an unknown id
    fails startup before anything is written — and every record insert goes
    through the typed-ref-verified batch path, so the seed is all-or-
    nothing. Seeding runs once per store (meta marker); reopens skip it.
    """
    if core.get_meta("registries_seeded"):
        return
    now = clock.now_rfc3339()
    seeded_ids = (
        {seed.id for seed in registries.principals}
        | {seed.id for seed in registries.resources}
        | {seed.id for seed in registries.commitments}
        | {seed.id for seed in registries.policies}
        | {seed.id for seed in registries.bindings}
        | {seed.id for seed in registries.authorizations}
    )
    all_ids = [
        seed.id
        for section in (
            registries.principals,
            registries.resources,
            registries.commitments,
            registries.policies,
            registries.bindings,
            registries.authorizations,
        )
        for seed in section
    ]
    if len(all_ids) != len(set(all_ids)):
        raise ManifestError("registry entries reuse an id across sections")

    def ref(record_id: str) -> RecordRef:
        return RecordRef(id=record_id, revision=1)

    def resolve(record_id: str, label: str) -> str:
        if record_id not in seeded_ids and core.get_record(record_id) is None:
            raise ManifestError(f"{label} cites unknown registry id {record_id!r}")
        return record_id

    def resolve_transform(transform_id: str, label: str) -> None:
        transform_spec(transform_id)  # unknown registry transform ids fail
        if transform_id not in seeded_transform_ids:
            raise ManifestError(
                f"{label} allows {transform_id!r} but the manifest seeds no"
                " fixture parameters for it"
            )

    seeded_transform_ids = {seed.id for seed in registries.transforms}

    for seed in registries.resources:
        resolve(seed.producer, f"resource {seed.id} producer")
        for commitment_id in seed.commitments:
            resolve(commitment_id, f"resource {seed.id} commitments")
    for seed in registries.commitments:
        for principal_id in (*seed.subjects, *seed.custodians):
            resolve(principal_id, f"commitment {seed.id} principal")
        for evidence_id in seed.policy_evidence:
            resolve(evidence_id, f"commitment {seed.id} policy_evidence")
        if seed.resource_id:
            # resource_id is a plain string field, so the store's typed-ref
            # backstop cannot catch it — resolve it here (review MINOR).
            resolve(seed.resource_id, f"commitment {seed.id} resource_id")
    for seed in registries.policies:
        for principal_id in seed.eligible_issuers:
            resolve(principal_id, f"policy {seed.id} eligible_issuers")
        for cited in (*seed.commitments, seed.resource_id):
            if cited:
                resolve(cited, f"policy {seed.id} reference")
        for transform_id in seed.allowed_transforms:
            resolve_transform(transform_id, f"policy {seed.id}")
    for seed in registries.bindings:
        resolve(seed.issuer, f"binding {seed.id} issuer")
        resolve(seed.evidence, f"binding {seed.id} evidence")
        resolve(seed.resource_id, f"binding {seed.id} resource_id")
        if seed.delegated_from:
            resolve(seed.delegated_from, f"binding {seed.id} delegated_from")
    for seed in registries.authorizations:
        for principal_id in seed.issuers:
            resolve(principal_id, f"authorization {seed.id} issuers")
        for binding_id in seed.bindings:
            resolve(binding_id, f"authorization {seed.id} bindings")
        resolve(seed.resource_id, f"authorization {seed.id} resource_id")
        for transform_id in seed.transforms:
            resolve_transform(transform_id, f"authorization {seed.id}")

    principals = [
        Principal(
            id=seed.id,
            revision=1,
            created_at=now,
            kind=_enum(seed.kind, PrincipalKind, f"principal {seed.id} kind"),
            verified_bindings=(),
            claimed_aliases=seed.aliases,
        )
        for seed in registries.principals
    ]
    fragments = []
    resource_payloads = []
    for seed in registries.resources:
        payload_id = f"payload:{seed.id}"
        resource_payloads.append((payload_id, seed.text, seed.id))
        fragments.append(
            ContentFragment(
                id=seed.id,
                revision=1,
                created_at=now,
                kind=FragmentKind.TEXT,
                payload_ref=payload_id,
                provenance=Provenance(
                    producer_ref=ref(seed.producer),
                    source_kind=_enum(
                        seed.source_kind,
                        SourceKind,
                        f"resource {seed.id} source_kind",
                    ),
                    origin_event_ref=None,
                    claimed_speaker_ref=None,
                    instruction_authority=InstructionAuthority.NONE,
                    integrity=Integrity.EXTERNAL_UNVERIFIED,
                    custodian_refs=(),
                    subject_refs=(),
                    original_audience_ref=None,
                    assertion_status=_enum(
                        seed.assertion_status,
                        AssertionStatus,
                        f"resource {seed.id} assertion_status",
                    ),
                    producer_coverage_ref=None,
                    model_claimed_parent_refs=(),
                    occurred_at=now,
                    observed_at=now,
                ),
                commitment_refs=tuple(ref(cid) for cid in seed.commitments),
                eligibility_status=EligibilityStatus.ACTIVE,
            )
        )
    commitments = [
        Commitment(
            id=seed.id,
            revision=1,
            created_at=now,
            subject_refs=tuple(ref(pid) for pid in seed.subjects),
            custodian_refs=tuple(ref(pid) for pid in seed.custodians),
            resource_id=seed.resource_id,
            source_scope=seed.source_scope,
            strength=_enum(seed.strength, CommitmentStrength, f"commitment {seed.id} strength"),
            internal_use_allowed=seed.internal_use_allowed,
            observable_influence_allowed=seed.observable_influence_allowed,
            expression_allowed=seed.expression_allowed,
            audience_predicate_id=seed.audience_predicate_id,
            policy_evidence_refs=tuple(ref(eid) for eid in seed.policy_evidence),
        )
        for seed in registries.commitments
    ]
    policies = [
        ResourcePolicy(
            id=seed.id,
            revision=1,
            created_at=now,
            resource_id=seed.resource_id,
            data_class=seed.data_class,
            approval_rule=_enum(
                seed.approval_rule,
                ApprovalRule,
                f"policy {seed.id} approval_rule",
            ),
            eligible_issuer_refs=tuple(ref(pid) for pid in seed.eligible_issuers),
            commitment_refs=tuple(ref(cid) for cid in seed.commitments),
            allowed_transform_ids=seed.allowed_transforms,
            policy_revision=1,
            status=RecordStatus.ACTIVE,
        )
        for seed in registries.policies
    ]
    bindings = [
        AuthorityBinding(
            id=seed.id,
            revision=1,
            created_at=now,
            issuer_ref=ref(seed.issuer),
            domain=_enum(seed.domain, AuthorityDomain, f"binding {seed.id} domain"),
            resource_id=seed.resource_id,
            evidence_ref=ref(seed.evidence),
            delegated_from=ref(seed.delegated_from) if seed.delegated_from else None,
            valid_until=seed.valid_until,
            revoked_at=None,
        )
        for seed in registries.bindings
    ]
    authorizations = [
        UseAuthorization(
            id=seed.id,
            revision=1,
            created_at=now,
            resource_id=seed.resource_id,
            issuer_refs=tuple(ref(pid) for pid in seed.issuers),
            binding_refs=tuple(ref(bid) for bid in seed.bindings),
            operations=tuple(
                _enum(op, ResourceOperation, f"authorization {seed.id} operations")
                for op in seed.operations
            ),
            permitted_purpose_classes=seed.purpose_classes,
            audience_class_bounds=seed.audience_bounds,
            maximum_precision=_enum(
                seed.precision,
                RequestedPrecision,
                f"authorization {seed.id} precision",
            ),
            allowed_transform_ids=seed.transforms,
            valid_until=seed.valid_until,
            status=RecordStatus.ACTIVE,
        )
        for seed in registries.authorizations
    ]
    batch = (*principals, *fragments, *commitments, *policies, *bindings, *authorizations)
    for payload_id, text, owner in resource_payloads:
        core.put_payload(payload_id, text, owner_ref=owner)
    core.put_records(batch)
    core.set_meta(
        "fixture_transforms",
        canonical_json_bytes(
            {
                seed.id: {pair.first: pair.second for pair in seed.parameters}
                for seed in registries.transforms
            }
        ).decode("utf-8"),
    )
    core.set_meta("registries_seeded", "1")


class ReplayRunner:
    """Deterministic replay over the full stack; produces an observer trace."""

    def __init__(self, manifest: RunManifest, run_dir: Path) -> None:
        self.manifest = manifest
        self.run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        self.clock = VirtualClock(manifest.clock_anchor)
        self.core = CoreStore(run_dir / "core.sqlite")
        self.transport = TransportStore(run_dir / "transport.sqlite")
        self.world = FakeWorld(run_dir / "world.sqlite")
        self.injector = FaultInjector()
        self._wire()

    # -- lifecycle controls (M3 D6, task 2.1) -------------------------------

    def restart_core(self) -> None:
        """Simulated core process loss: core.sqlite reopens (the runtime
        epoch advances durably and RUNNING work returns READY with budgets
        and dedup keys retained) and every core-bound component rewires.
        The fresh main port starts at the accepted-step count, so a
        crashed, unaccepted step replays at the same index — identical
        replay, never a skipped or doubled step."""
        self.core.close()
        self.core = CoreStore(self.run_dir / "core.sqlite")
        self._wire(replay_script=True)

    def restart_transport(self) -> None:
        """Simulated sidecar process loss: transport.sqlite reopens (the
        transport generation advances durably); outbox and attempts
        survive, and the sidecar continues on the new generation."""
        self.transport.close()
        self.transport = TransportStore(self.run_dir / "transport.sqlite")
        self.sidecar.transport = self.transport

    def advance_clock(self, delta_ms: int) -> int:
        return self.clock.advance_ms(delta_ms)

    def fault_at(self, checkpoint: CrashCheckpoint, *, times: int = 1) -> None:
        self.injector.arm(checkpoint, times=times)

    def _accepted_step_count(self) -> int:
        row = self.core._conn.execute(
            "SELECT COALESCE(SUM(main_steps_used), 0) FROM core_work"
        ).fetchone()
        return int(row[0])

    def _wire(self, *, replay_script: bool = False) -> None:
        known = self.core.get_record("identity:agent")
        if known is None:
            identity, audience, capability, sink_ref = Coordinator.bootstrap(
                self.core,
                self.clock,
                destination_id=self.manifest.destination_id,
                allowed_destination_ids=self.manifest.allowed_destinations,
            )
        else:
            assert isinstance(known, AgentIdentity)
            audience = self.core.get_record(f"audience:{self.manifest.destination_id}")
            capability = self.core.get_record("capability:social")
            sink_ref = RecordRef(id="sink:scripted", revision=1)
            assert isinstance(audience, AudienceSnapshot)
            assert isinstance(capability, CapabilityEnvelope)
            identity, _, _ = known, audience, capability
        seed_registries(self.core, self.clock, self.manifest.registries)
        self.context_memory = ContextMemory(self.core, self.clock, self.injector)
        if replay_script or not hasattr(self, "main_port"):
            self.main_port = ScriptedMainModel(
                self.manifest.steps, start_calls=self._accepted_step_count()
            )
        self.gate = CommitGate(self.core, self.clock)
        self.sidecar = FakeSidecar(self.core, self.transport, self.world, self.clock, self.injector)
        self.coordinator = Coordinator(
            self.core,
            self.clock,
            self.context_memory,
            self.main_port,
            self.gate,
            self.sidecar,
            identity=identity,
            audience=audience,
            capability=capability,
            sink_ref=sink_ref,
            # The manifest's registered purpose vocabulary, wired at
            # runtime. The mechanical class is unioned in so manifests
            # without a purpose_classes section still admit the class the
            # runtime's mechanical intents — and section-less scripted
            # CREATE_INTENT steps — use (bead njy).
            config=TurnConfig(
                purpose_classes=frozenset(self.manifest.registries.purpose_classes)
                | {MECHANICAL_PURPOSE_CLASS}
            ),
            injector=self.injector,
        )

    def run(self) -> dict[str, Any]:
        trace: dict[str, Any] = {
            "fixture": self.manifest.fixture_id,
            "ingested": [],
            "turns": [],
            "world": [],
            "faults": [],
        }
        ingest_faults = {
            step.after_event: step
            for step in self.manifest.faults
            if step.checkpoint == "AFTER_INGEST_BEFORE_ACK"
        }
        fired_ingest: set[str] = set()
        for index, event in enumerate(sorted(self.manifest.events, key=lambda e: e.at_ms), start=1):
            self.clock.advance_to(event.at_ms)
            # an ingest fault fires once per event id, at its first journal
            # entry — an edit sequence sharing the id does not re-fire it
            fault = ingest_faults.get(event.event_id)
            if fault is not None and event.event_id in fired_ingest:
                fault = None
            if fault is not None:
                fired_ingest.add(event.event_id)
            if fault is not None and fault.checkpoint == "AFTER_INGEST_BEFORE_ACK":
                checkpoint = CrashCheckpoint(fault.checkpoint)
                self.injector.arm(checkpoint)
                ingested = False
                try:
                    self._append_event(index, event)
                    receipt = self.coordinator.ingest(self.transport, consumer_id="core-1")
                    trace["ingested"].append(
                        {"seq": index, "event": event.event_id, "applied": list(receipt.applied)}
                    )
                    ingested = True
                except InjectedCrash:
                    pass
                self.injector.disarm(checkpoint)
                self._apply_restart(fault.restart)
                if not ingested:
                    # the row and cursor committed before the crash, only
                    # the acknowledgement was lost — re-ingest is idempotent
                    receipt = self.coordinator.ingest(self.transport, consumer_id="core-1")
                    trace["ingested"].append(
                        {
                            "seq": index,
                            "event": event.event_id,
                            "applied": list(receipt.applied),
                            "recovered_ingest": True,
                        }
                    )
                trace["faults"].append(self._fault_entry(fault, fired=True))
                continue
            self._append_event(index, event)
            receipt = self.coordinator.ingest(self.transport, consumer_id="core-1")
            trace["ingested"].append(
                {"seq": index, "event": event.event_id, "applied": list(receipt.applied)}
            )
        # Ordinary pending events coalesce per trigger key (one READY work
        # item), so all events ingest before turns drain; the fault plan
        # then fires its remaining checkpoints inside the drain phase.
        self._drain_with_faults(trace)
        self._fire_due_timers(trace)
        self._drain(trace)
        self._reconcile_inflight(trace)
        trace["world"] = self._world_rows()
        return trace

    def _drain_with_faults(self, trace: dict[str, Any]) -> None:
        """Run the drain phase with the manifest's faults applied
        sequentially: each fault's checkpoint is armed for one drain
        attempt, a crash restarts the named domain, and the next attempt
        continues — accepted steps never replay, crashed unaccepted steps
        replay at the same index."""
        remaining = [
            step for step in self.manifest.faults if step.checkpoint != "AFTER_INGEST_BEFORE_ACK"
        ]
        while True:
            fault = remaining[0] if remaining else None
            if fault is not None:
                self.injector.arm(CrashCheckpoint(fault.checkpoint))
            try:
                self._drain(trace)
                if fault is None:
                    return
                # the checkpoint never fired in this drain: record the
                # divergence honestly and try the next fault in list order
                self.injector.disarm(CrashCheckpoint(fault.checkpoint))
                trace["faults"].append(self._fault_entry(fault, fired=False))
                remaining.pop(0)
                continue
            except InjectedCrash:
                assert fault is not None  # armed above; the crash consumed it
                self.injector.disarm(CrashCheckpoint(fault.checkpoint))
                self._apply_restart(fault.restart)
                trace["faults"].append(self._fault_entry(fault, fired=True))
                remaining.pop(0)

    def _apply_restart(self, restart: str) -> None:
        if restart == "core":
            self.restart_core()
        elif restart == "transport":
            self.restart_transport()

    @staticmethod
    def _fault_entry(fault: FaultPlanStep, *, fired: bool) -> dict[str, object]:
        return {
            "after_event": fault.after_event,
            "checkpoint": fault.checkpoint,
            "restart": fault.restart,
            "fired": fired,
        }

    def _reconcile_inflight(self, trace: dict[str, Any]) -> None:
        """Query first, never guess (§6.2): after the run settles, every
        outbox row still past the linearization point (DISPATCH_STARTED at
        restart, or UNKNOWN) resolves only through positive world
        evidence; INDETERMINATE rows settle conservatively as UNKNOWN."""
        trace.setdefault("reconciled", [])
        rows = self.transport._conn.execute(
            "SELECT logical_effect_id, state FROM transport_outbox"
            " WHERE state IN ('DISPATCH_STARTED', 'UNKNOWN')"
        ).fetchall()
        for row in rows:
            verdict = self.sidecar.reconcile(row["logical_effect_id"])
            trace["reconciled"].append(
                {"effect": row["logical_effect_id"], "verdict": str(verdict)}
            )

    def _append_event(self, seq: int, event: FixtureEvent) -> None:
        fragment_id = f"fragment:{event.event_id}"
        payload_id = f"payload:{event.event_id}:r{event.source_revision}"
        if event.kind == "MESSAGE_DELETED":
            fragment_refs: tuple[str, ...] = ()
        else:
            self.core.put_payload(payload_id, event.text, owner_ref=event.event_id)
            self.core.put_record(
                ContentFragment(
                    id=fragment_id,
                    revision=event.source_revision,
                    created_at=self.clock.now_rfc3339(),
                    kind=FragmentKind.TEXT,
                    payload_ref=payload_id,
                    provenance=Provenance(
                        producer_ref=RecordRef(id="principal:agent", revision=1),
                        source_kind=SourceKind.SOCIAL_MESSAGE,
                        origin_event_ref=None,
                        claimed_speaker_ref=None,
                        instruction_authority=InstructionAuthority.NONE,
                        integrity=Integrity.EXTERNAL_UNVERIFIED,
                        custodian_refs=(),
                        subject_refs=(),
                        original_audience_ref=None,
                        assertion_status=AssertionStatus.OBSERVED_STATEMENT,
                        producer_coverage_ref=None,
                        model_claimed_parent_refs=(),
                        occurred_at=self.clock.now_rfc3339(),
                        observed_at=self.clock.now_rfc3339(),
                    ),
                    commitment_refs=(),
                    eligibility_status=EligibilityStatus.ACTIVE,
                )
            )
            fragment_refs = (fragment_id,)
        self.transport.append_events(
            (
                JournalEntry(
                    journal_seq=seq,
                    source_event_id=event.event_id,
                    source_revision=event.source_revision,
                    kind=event.kind,
                    payload_ref=payload_id,
                    fragment_refs=fragment_refs,
                    recovery="COMPLETE",
                ),
            )
        )

    def _drain(self, trace: dict[str, Any]) -> None:
        for work_id in self.coordinator.ready_work_ids():
            result = self.coordinator.run_turn(work_id)
            trace["turns"].append(
                {
                    "work": work_id,
                    "outcome": result.outcome.kind.value,
                    "reasons": [code.value for code in result.outcome.reason_codes],
                    "committed": result.receipt_ref is not None,
                }
            )

    def _fire_due_timers(self, trace: dict[str, Any]) -> None:
        """Deferred intents wake exactly once: a deduped TIMER_DUE occurrence
        at the recorded due time (handoff §6.4 fixed-time defer). An expired
        deferral never fires — the intent is abandoned without a visible
        effect instead (§8)."""
        rows = self.core._conn.execute(
            "SELECT record_id, payload_json FROM core_records"
            " WHERE record_type = 'Intent' AND is_current = 1 ORDER BY record_id"
        ).fetchall()
        next_seq = self.transport.max_journal_seq + 1
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("state") != IntentState.DEFERRED.value:
                continue
            due_at = payload.get("due_at")
            if not due_at:
                continue
            due_ms = self.clock.to_ms(due_at)
            if due_ms > self.clock.now_ms():
                # the clock never moves backwards (§3): a due already past
                # (coalesced ingest advanced past it) fires at once
                self.clock.advance_to(due_ms)
            expires_at = payload.get("expires_at")
            if expires_at and self.clock.to_ms(expires_at) < self.clock.now_ms():
                # expiry abandons without a visible effect (§8); the state
                # check inside guards the §6.1 machine
                self.coordinator.abandon_intent(RecordRef(id=row["record_id"], revision=1))
                continue
            timer_event_id = f"timer:{row['record_id']}:{due_at}:{payload['endorsement_revision']}"
            if self.transport.event_exists(timer_event_id, 1):
                # One occurrence per (intent, due, endorsement) tuple, ever
                # (§8): a re-defer to the same instant, or a restart while
                # the wake is still pending, must not re-append the
                # consumed journal entry — the unique constraint would
                # crash the fire pass on every later attempt.
                continue
            self.transport.append_events(
                (
                    JournalEntry(
                        journal_seq=next_seq,
                        source_event_id=timer_event_id,
                        source_revision=1,
                        kind="TIMER_DUE",
                        payload_ref=f"payload:{timer_event_id}",
                        fragment_refs=(),
                        recovery="COMPLETE",
                    ),
                )
            )
            next_seq += 1
            receipt = self.coordinator.ingest(self.transport, consumer_id="core-1")
            trace["ingested"].append(
                {"seq": next_seq - 1, "event": timer_event_id, "applied": list(receipt.applied)}
            )

    def _world_rows(self) -> list[dict[str, Any]]:
        rows = self.world._conn.execute(
            "SELECT attempt_id, effect_id, payload_bytes, visible_at_ms FROM world_effects"
            " ORDER BY visible_at_ms"
        ).fetchall()
        return [dict(row) for row in rows]
