"""The four M1 outcome demonstrations via the replay runner + CLI
(ROADMAP M1 exit evidence): ordinary message, reaction, explicit no
action, and timer-driven voluntary initiation."""

from __future__ import annotations

import hashlib
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
    # the content digest binds the target's real payload bytes (bead
    # hermes-agora-ecy), never the placeholder zeros
    expected_digest = hashlib.sha256(b"hermes, look at this project note").hexdigest()
    assert f'"target_content_digest":"{expected_digest}"' in world_bytes
    assert f'"target_content_digest":"{"0" * 64}"' not in world_bytes


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
    # the wake RESUMED the deferred intent (bead 9xk): the timer work row
    # is bound to it and it is OPEN again, not stranded DEFERRED
    wake = runner.core._conn.execute(
        "SELECT intent_ref FROM core_work WHERE trigger_key LIKE 'timer:%'"
    ).fetchone()
    assert wake is not None
    from agora.contracts import Intent

    intent = runner.core.get_record(wake["intent_ref"])
    assert isinstance(intent, Intent) and intent.state.value == "OPEN"


def test_expired_deferral_abandons_without_firing(tmp_path: Path) -> None:
    """§8: intent expiry abandons without a visible effect — with the clock
    already past defer_expires_at when the timer's due arrives, no
    TIMER_DUE fires, the intent is abandoned, and no wake work exists."""
    payload = {
        "manifest_version": 1,
        "fixture_id": "m1-defer-expiry",
        "clock_anchor": ANCHOR,
        "destination_id": "room:public",
        "allowed_destinations": ["room:public"],
        "events": [
            {
                "at_ms": 100,
                "event_id": "evt:greeting",
                "kind": "MESSAGE_CREATED",
                "text": "hermes, look at this project note",
            },
            {
                "at_ms": 60_000,
                "event_id": "evt:later",
                "kind": "MESSAGE_CREATED",
                "text": "much later",
            },
        ],
        "steps": [
            {
                "kind": "SUBMIT_PLAN",
                "intent_ref": INTENT,
                "disposition": "DEFER",
                "effects": [],
                "declared_fact_refs": [],
                "defer_until": "2026-10-10T12:00:40Z",
                "defer_expires_at": "2026-10-10T12:00:50Z",
            },
            {"kind": "FINISH", "outcome": "NO_ACTION"},
        ],
    }
    runner = ReplayRunner(RunManifest.from_payload(payload), tmp_path / "run")
    trace = runner.run()

    assert not [entry for entry in trace["ingested"] if entry["event"].startswith("timer:")]
    assert [turn["outcome"] for turn in trace["turns"]] == ["DEFERRED", "NO_ACTION"]
    from agora.contracts import IntentState

    rows = runner.core._conn.execute(
        "SELECT payload_json FROM core_records WHERE record_type = 'Intent' AND is_current = 1"
    ).fetchall()
    states = [IntentState(json.loads(row["payload_json"])["state"]) for row in rows]
    assert IntentState.DEFERRED not in states
    assert IntentState.ABANDONED in states
    assert (
        runner.core._conn.execute(
            "SELECT count(*) FROM core_work WHERE trigger_key LIKE 'timer:%'"
        ).fetchone()[0]
        == 0
    )


def test_refired_timer_tuple_stays_one_occurrence(tmp_path: Path) -> None:
    """§8 one occurrence per (intent, due, endorsement) tuple, ever: a
    wake that re-defers to the same fixed instant leaves the intent
    DEFERRED under a tuple whose TIMER_DUE is already consumed — a later
    fire pass (a restart re-entering the timer phase) must skip it
    instead of re-appending the journal entry and crashing on the unique
    constraint."""
    defer_step: dict[str, object] = {
        "kind": "SUBMIT_PLAN",
        "intent_ref": INTENT,
        "disposition": "DEFER",
        "effects": [],
        "declared_fact_refs": [],
        "defer_until": "2026-10-10T12:00:40Z",
        "defer_expires_at": "2026-10-10T12:01:40Z",
    }
    payload = manifest_payload([defer_step, dict(defer_step)])
    run_dir = tmp_path / "run"
    trace = ReplayRunner(RunManifest.from_payload(payload), run_dir).run()
    assert [turn["outcome"] for turn in trace["turns"]] == ["DEFERRED", "DEFERRED"]
    assert len([e for e in trace["ingested"] if e["event"].startswith("timer:")]) == 1
    timer_events = [e for e in trace["ingested"] if e["event"].startswith("timer:")]
    consumed_id = timer_events[0]["event"]

    # a restart re-opens the same stores and re-enters the timer phase
    runner2 = ReplayRunner(RunManifest.from_payload(payload), run_dir)
    runner2.clock.advance_ms(120_000)  # past the due, inside the expiry
    trace2: dict[str, object] = {"ingested": [], "turns": [], "world": [], "faults": []}
    runner2._fire_due_timers(trace2)  # type: ignore[arg-type]
    assert [e for e in trace2["ingested"] if isinstance(e, dict) and "event" in e] == []
    assert runner2.transport.event_exists(consumed_id, 1)
    assert runner2.transport.max_journal_seq == 2  # nothing re-appended


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
