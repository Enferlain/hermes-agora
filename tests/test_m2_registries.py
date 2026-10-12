"""M2 task 1.2: manifest registries seed the fixture resource classes.

Verify clause: a seeded replay boots and the registries round-trip from
core.records; unknown registry ids fail startup with nothing seeded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agora.contracts import Commitment, ContentFragment, ResourcePolicy
from agora.runner import ManifestError, ReplayRunner, RunManifest
from tests.fixtures_m2 import (
    AGENT_PREF_RESOURCE,
    FINANCE_RESOURCE,
    HUMAN,
    JOINT_RESOURCE,
    PROJECT_FACT_RESOURCE,
    THIRD_PARTY_RESOURCE,
    five_class_registries,
    manifest_with_registries,
)

SEEDED_TYPES = {
    HUMAN: "Principal",
    PROJECT_FACT_RESOURCE: "ContentFragment",
    AGENT_PREF_RESOURCE: "ContentFragment",
    FINANCE_RESOURCE: "ContentFragment",
    THIRD_PARTY_RESOURCE: "ContentFragment",
    JOINT_RESOURCE: "ContentFragment",
    "commit:finance-strong": "Commitment",
    "policy:finance": "ResourcePolicy",
    "binding:human-finance": "AuthorityBinding",
    "use:finance-fields": "UseAuthorization",
    "commit:joint-strong": "Commitment",
    "policy:joint": "ResourcePolicy",
    "use:joint-expression": "UseAuthorization",
}


def boot(tmp_path: Path, payload: dict) -> ReplayRunner:
    manifest = RunManifest.from_payload(payload)
    runner = ReplayRunner(manifest, tmp_path / "run")
    runner.run()
    return runner


def test_seeded_replay_boots_and_roundtrips(tmp_path: Path) -> None:
    runner = boot(tmp_path, manifest_with_registries(five_class_registries()))
    core = runner.core
    for record_id, expected_type in SEEDED_TYPES.items():
        record = core.get_record(record_id)
        assert record is not None, record_id
        assert expected_type == record.TYPE_NAME, record_id
    # protected fact carries its STRONG commitment; policy/approval round-trip
    fragment = core.get_record(FINANCE_RESOURCE)
    assert isinstance(fragment, ContentFragment)
    assert [ref.id for ref in fragment.commitment_refs] == ["commit:finance-strong"]
    commitment = core.get_record("commit:finance-strong")
    assert isinstance(commitment, Commitment)
    assert commitment.strength.value == "STRONG"
    assert commitment.expression_allowed is False
    policy = core.get_record("policy:finance")
    assert isinstance(policy, ResourcePolicy)
    assert policy.approval_rule.value == "SINGLE_ISSUER"
    assert policy.allowed_transform_ids == ("FIXED_AFFORDABILITY_PREDICATE",)
    joint_policy = core.get_record("policy:joint")
    assert isinstance(joint_policy, ResourcePolicy)
    assert joint_policy.approval_rule.value == "ALL_LISTED"
    # fixture-fixed transform parameters persist as config meta
    stored = json.loads(core.get_meta("fixture_transforms") or "{}")
    assert stored == {"FIXED_AFFORDABILITY_PREDICATE": {"price": 30}}
    # resource text lives out-of-line and is retrievable
    assert core.get_payload(f"payload:{FINANCE_RESOURCE}") is not None
    # the replay itself completed (quiet finish on the boot event)
    assert runner.transport.outbox_state("evt-boot") is None


def test_unknown_registry_id_fails_startup_with_nothing_seeded(tmp_path: Path) -> None:
    registries = five_class_registries()
    registries["commitments"][1]["policy_evidence"] = ["policy:ghost"]
    payload = manifest_with_registries(registries)
    manifest = RunManifest.from_payload(payload)  # parse-level checks pass...
    with pytest.raises(ManifestError, match="policy:ghost"):
        ReplayRunner(manifest, tmp_path / "run")
    # nothing seeded: open the store the runner created and check
    from agora.stores import CoreStore

    core = CoreStore(tmp_path / "run" / "core.sqlite")
    assert core.get_meta("registries_seeded") is None
    for record_id in SEEDED_TYPES:
        assert core.get_record(record_id) is None, record_id
    core.close()


def test_reopen_does_not_reseed_and_stays_consistent(tmp_path: Path) -> None:
    runner = boot(tmp_path, manifest_with_registries(five_class_registries()))
    created = {record_id: runner.core.get_record(record_id) for record_id in SEEDED_TYPES}
    runner.core.close()
    runner.transport.close()
    runner.world.close()
    # a second runner over the same directory must not re-seed (idempotence
    # guard) and the records must be unchanged
    manifest = RunManifest.from_payload(manifest_with_registries(five_class_registries()))
    again = ReplayRunner(manifest, tmp_path / "run")
    for record_id, original in created.items():
        assert original is not None
        record = again.core.get_record(record_id)
        assert record == original, record_id
        assert again.core.record_revisions(record_id) == (1,)


def test_manifest_version_gates_the_registries_section(tmp_path: Path) -> None:
    payload = manifest_with_registries(five_class_registries())
    payload["manifest_version"] = 1
    with pytest.raises(ManifestError, match="manifest_version 2"):
        RunManifest.from_payload(payload)


def test_unregistered_transform_id_fails_at_parse(tmp_path: Path) -> None:
    registries = five_class_registries()
    registries["transforms"][0]["id"] = "DYNAMIC_SUMMARIZE"
    payload = manifest_with_registries(registries)
    with pytest.raises(Exception, match="fixed registry"):
        RunManifest.from_payload(payload)


def test_transform_parameter_set_must_match_the_spec(tmp_path: Path) -> None:
    registries = five_class_registries()
    registries["transforms"][0]["parameters"] = [["price", 30], ["headroom", 5]]
    payload = manifest_with_registries(registries)
    with pytest.raises(Exception, match="parameters must be exactly"):
        RunManifest.from_payload(payload)
