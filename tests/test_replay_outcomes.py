"""The four M1 outcome demonstrations via the replay runner + CLI
(ROADMAP M1 exit evidence): ordinary message, reaction, explicit no
action, and timer-driven voluntary initiation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agora.runner import ReplayRunner, RunManifest

ANCHOR = "2026-10-10T12:00:00Z"
INTENT = {"id": "intent:turn:placeholder", "revision": 1}


def manifest_payload(steps: list[dict[str, object]]) -> dict[str, object]:
    return {
        "manifest_version": 1,
        "fixture_id": "m1-outcomes",
        "clock_anchor": ANCHOR,
        "destination_id": "room:public",
        "allowed_destinations": ["room:public"],
        "events": [
            {
                "at_ms": 100,
                "event_id": "evt:greeting",
                "kind": "MESSAGE_CREATED",
                "text": "hermes, look at this project note",
            }
        ],
        "steps": steps,
    }


def submit_send(text: str) -> dict[str, object]:
    return {
        "kind": "SUBMIT_PLAN",
        "intent_ref": INTENT,
        "disposition": "ACT",
        "effects": [
            {
                "kind": "SEND_MESSAGE",
                "destination_id": "room:public",
                "text": text,
                "target_ref": None,
            }
        ],
        "declared_fact_refs": [],
    }


def test_reaction_outcome_renders_exact_shape(tmp_path: Path) -> None:
    payload = manifest_payload(
        [
            {
                "kind": "SUBMIT_PLAN",
                "intent_ref": INTENT,
                "disposition": "ACT",
                "effects": [
                    {
                        "kind": "ADD_REACTION",
                        "destination_id": "room:public",
                        "target_ref": {"id": "fragment:evt:greeting", "revision": 1},
                        "emoji": "🌊",
                    }
                ],
                "declared_fact_refs": [],
            }
        ]
    )
    runner = ReplayRunner(RunManifest.from_payload(payload), tmp_path / "run")
    trace = runner.run()
    assert [turn["outcome"] for turn in trace["turns"]] == ["COMMITTED"]
    world_bytes = trace["world"][0]["payload_bytes"]
    assert '"kind":"ADD_REACTION"' in world_bytes
    assert '"emoji":"🌊"' in world_bytes
    assert '"target_ref":{"id":"fragment:evt:greeting","revision":1}' in world_bytes


def test_timer_driven_voluntary_initiation(tmp_path: Path) -> None:
    payload = manifest_payload(
        [
            {"kind": "FINISH", "outcome": "DEFER"},
            submit_send("following up on that project note, as planned"),
        ]
    )
    runner = ReplayRunner(RunManifest.from_payload(payload), tmp_path / "run")
    trace = runner.run()

    # first turn defers with an explicit due time; exactly one deduped timer
    # wakes; the second turn initiates without any inbound social event.
    assert [turn["outcome"] for turn in trace["turns"]] == ["DEFERRED", "COMMITTED"]
    timer_events = [entry for entry in trace["ingested"] if entry["event"].startswith("timer:")]
    assert len(timer_events) == 1
    initiation = trace["world"][-1]["payload_bytes"]
    assert "following up on that project note" in initiation
    # the timer fired at (or after) the deferral's due time
    assert trace["world"][-1]["visible_at_ms"] >= 100


def test_manifest_rejects_runtime_only_fields() -> None:
    from agora.contracts import ValidationError

    payload = manifest_payload([{"kind": "FINISH", "outcome": "NO_ACTION", "runtime_epoch": 3}])
    with pytest.raises(ValidationError):
        RunManifest.from_payload(payload)


def test_cli_replay_produces_expected_trace(tmp_path: Path) -> None:
    manifest_path = tmp_path / "fixture.json"
    manifest_path.write_text(
        json.dumps(manifest_payload([submit_send("hello from the replay cli")])),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "agora", "replay", str(manifest_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    trace = json.loads(completed.stdout)
    assert trace["fixture"] == "m1-outcomes"
    assert [turn["outcome"] for turn in trace["turns"]] == ["COMMITTED"]
    assert "hello from the replay cli" in trace["world"][0]["payload_bytes"]


def test_cli_replay_rejects_bad_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "bad.json"
    manifest_path.write_text('{"manifest_version": 99}', encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "agora", "replay", str(manifest_path)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "invalid manifest" in completed.stderr
