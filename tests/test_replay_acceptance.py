"""M3 replay acceptance (beads hermes-agora-usf.2, tasks 2.1 through 2.5).

Runner lifecycle controls and fencing (I10), the crash-boundary battery at
every named checkpoint with conservative recovery (§6.2), replay dedup and
determinism (I16), and the three handoff §10 stories end-to-end with
restarts — each run twice, byte-identical traces.

I01-I16 matrix (task 2.5) — every invariant mapped to its focused tests:

- I01 intents without grants:      test_wiring_turn (autonomous turns),
                                   test_review_routing (derived-intent paths)
- I02 no external authority       test_authority_evaluator (issuer/eligibility
  upgrade:                          battery), adversarial battery provenance
- I03 issuer-policy approvals:     test_authority_evaluator quorum cases
- I04 actual inputs determine     test_adversarial_battery (declared-parent
  constraints:                      omission), test_review_regressions
                                   (production coverage-driven routing)
- I05 compute admission +         test_request_access (envelope sinks and
  envelope match:                   supplied refs), adversarial battery
                                   (no protected bytes to envelopes)
- I06 gate-only effect path:       test_wiring_turn (model bad ref typed
                                   denial), test_contracts_records
                                   (fabricated authorization rejected)
- I07 REQUIRED holds, NONE never  test_review_regressions (REVIEW_TIMEOUT,
  forced:                           descriptors-only), test_wiring_turn
                                   (quiet turns)
- I08 versions bound through      test_dispatch_m3 (policy/target/audience/
  dispatch:                         capability/snapshot holds, post-
                                   linearization conservatism)
- I09 stable ids + acknowledged   test_dispatch_m3 (supersession battery),
  cancellation:                     story 3 below
- I10 one generation, fenced:     this file (lease fencing units, restart
                                   epoch/generation advance, battery)
- I11 UNKNOWN never blind:        test_dispatch_m3 (retry/reconcile rules),
                                   this file (AFTER_DISPATCH_START and
                                   AFTER_WORLD battery cases)
- I12 no permission widening:     test_adversarial_battery (private→public
                                   switching, room containment)
- I13 no fallback visibility:     test_wiring_turn (quiet/budget turns
                                   create no outbox noise)
- I14 corrections invalidate:     test_memory_lifecycle (transitive
                                   invalidation), test_dispatch_m3 (snapshot
                                   invalidation hold), this file
                                   (DURING_CORRECTION_JOB atomicity)
- I15 atomic release limits:      test_dispatch_m3 (ledger battery)
- I16 dedup + continuous identity: test_stores_ingestion (cursor dedup,
                                   explicit gaps, crash-before-ack), this
                                   file (timer dedup, restart identity,
                                   byte-identical re-runs)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agora.contracts import DependencyKind, RecordRef
from agora.fake_sidecar import SidecarError
from agora.ports import CrashCheckpoint, InjectedCrash
from agora.runner import ReplayRunner, RunManifest
from tests.fixtures_m2 import FINANCE_RESOURCE, five_class_registries

ANCHOR = "2026-10-10T12:00:00Z"
INTENT = {"id": "intent:turn:placeholder", "revision": 1}


def _manifest(
    events: list[dict[str, object]],
    steps: list[dict[str, object]],
    *,
    faults: list[dict[str, object]] | None = None,
    registries: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "manifest_version": 2,
        "fixture_id": "m3-acceptance",
        "clock_anchor": ANCHOR,
        "destination_id": "room:public",
        "allowed_destinations": ["room:public"],
        "events": events,
        "steps": steps,
    }
    if faults is not None:
        payload["faults"] = faults
    if registries is not None:
        payload["registries"] = registries
    return payload


def _message(at_ms: int, event_id: str, text: str, **extra: object) -> dict[str, object]:
    return {"at_ms": at_ms, "event_id": event_id, "kind": "MESSAGE_CREATED", "text": text, **extra}


def _send(text: str) -> dict[str, object]:
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


def _reaction(target: dict[str, object], emoji: str = "👋") -> dict[str, object]:
    return {
        "kind": "SUBMIT_PLAN",
        "intent_ref": INTENT,
        "disposition": "ACT",
        "effects": [
            {
                "kind": "ADD_REACTION",
                "destination_id": "room:public",
                "target_ref": target,
                "emoji": emoji,
            }
        ],
        "declared_fact_refs": [],
    }


def _finish(outcome: str) -> dict[str, object]:
    return {"kind": "FINISH", "outcome": outcome}


def _run(payload: dict[str, object], run_dir: Path) -> dict[str, Any]:
    return ReplayRunner(RunManifest.from_payload(payload), run_dir).run()


def _trace_json(trace: dict[str, Any]) -> str:
    return json.dumps(trace, sort_keys=True)


def _outbox(runner: ReplayRunner, effect_suffix: str) -> str | None:
    rows = runner.transport._conn.execute(
        "SELECT logical_effect_id, state FROM transport_outbox"
    ).fetchall()
    for row in rows:
        if row["logical_effect_id"].endswith(effect_suffix):
            return row["state"]
    return None


# -- 2.1 lifecycle controls and fencing (I10) ---------------------------------


def test_restart_controls_advance_epoch_and_generation(tmp_path: Path) -> None:
    runner = ReplayRunner(
        RunManifest.from_payload(_manifest([_message(100, "evt:1", "hello")], [_send("hi")])),
        tmp_path / "run",
    )
    epoch_before = runner.core.runtime_epoch
    generation_before = runner.transport.transport_generation
    runner.restart_core()
    runner.restart_transport()
    assert runner.core.runtime_epoch == epoch_before + 1
    assert runner.transport.transport_generation == generation_before + 1


def test_lease_fencing_raises_the_typed_codes(tmp_path: Path) -> None:
    """I10: a stale epoch, a stale generation, an expired or an
    already-consumed lease can never start dispatch — each refusal is the
    typed code, and nothing partial commits."""
    from tests.fixtures_review import ReviewStack

    stack = ReviewStack(tmp_path)

    def lease_row(lease_id: str, *, epoch: int, generation: int, expires: int, state: str) -> None:
        stack.core._conn.execute(
            "INSERT INTO core_dispatch_leases (lease_id, attempt_id, effect_id, auth_ref,"
            " payload_digest, runtime_epoch, transport_generation, expires_at_ms, state)"
            " VALUES (?, ?, 'e', '{}', '0', ?, ?, ?, ?)",
            (lease_id, lease_id, epoch, generation, expires, state),
        )

    now = stack.clock.now_ms()
    epoch = stack.core.runtime_epoch
    generation = stack.transport.transport_generation
    lease_row(
        "lease:stale-epoch",
        epoch=epoch + 1,
        generation=generation,
        expires=now + 5000,
        state="ISSUED",
    )
    lease_row(
        "lease:stale-generation",
        epoch=epoch,
        generation=generation + 1,
        expires=now + 5000,
        state="ISSUED",
    )
    lease_row("lease:expired", epoch=epoch, generation=generation, expires=now - 1, state="ISSUED")
    lease_row(
        "lease:consumed", epoch=epoch, generation=generation, expires=now + 5000, state="CONSUMED"
    )

    def fenced_code(lease_id: str, generation_arg: int) -> str:
        with pytest.raises(SidecarError) as excinfo, stack.core.transaction() as connection:
            stack.sidecar._fence_consume_lease_tx(connection, lease_id, generation_arg)
        assert excinfo.value.code is not None
        return excinfo.value.code.value

    assert fenced_code("lease:stale-epoch", generation) == "STALE_EPOCH"
    assert fenced_code("lease:stale-generation", generation) == "STALE_GENERATION"
    assert fenced_code("lease:expired", generation) == "LEASE_EXPIRED"
    assert fenced_code("lease:consumed", generation) == "LEASE_CONSUMED"


def test_crash_between_model_and_accept_replays_the_same_step(tmp_path: Path) -> None:
    payload = _manifest(
        [_message(100, "evt:1", "please continue")],
        [_send("recovered and committed")],
        faults=[{"after_event": "evt:1", "checkpoint": "AFTER_MODEL_BEFORE_ACCEPT"}],
    )
    trace = _run(payload, tmp_path / "run")
    assert [turn["outcome"] for turn in trace["turns"]] == ["COMMITTED"]
    assert len(trace["world"]) == 1
    runner = ReplayRunner(RunManifest.from_payload(payload), tmp_path / "run2")
    trace2 = runner.run()
    assert _trace_json(trace) == _trace_json(trace2)  # identical replay


# -- 2.2 crash-boundary battery -------------------------------------------------

BATTERY_EVENTS = [_message(100, "evt:1", "battery event")]
BATTERY_STEPS = [
    _send("battery payload"),
    _send("battery payload, requeued after restart"),
]


@pytest.mark.parametrize(
    ("checkpoint", "expected_outcomes", "expected_world", "expected_outbox"),
    [
        ("AFTER_INGEST_BEFORE_ACK", ["COMMITTED"], 1, "SENT"),
        ("AFTER_MODEL_BEFORE_ACCEPT", ["COMMITTED"], 1, "SENT"),
        ("AFTER_RESERVE_BEFORE_LEASE_RETURN", ["COMMITTED"], 1, "SENT"),
        ("AFTER_LEASE_CONSUME_BEFORE_DISPATCH_START", ["COMMITTED"], 1, "SENT"),
        # DISPATCH_STARTED at restart with no world evidence: never a
        # guessed no-send and never a replacement retry — conservative
        # UNKNOWN via reconciliation (I11)
        ("AFTER_DISPATCH_START_BEFORE_WORLD", ["HELD"], 0, "UNKNOWN"),
        # the world call committed but the receipt did not: positive
        # evidence resolves the in-flight effect to SENT
        ("AFTER_WORLD_BEFORE_RECEIPT", ["HELD"], 1, "SENT"),
    ],
)
def test_crash_boundary_battery(
    tmp_path: Path,
    checkpoint: str,
    expected_outcomes: list[str],
    expected_world: int,
    expected_outbox: str,
) -> None:
    payload = _manifest(
        BATTERY_EVENTS,
        BATTERY_STEPS,
        faults=[{"after_event": "evt:1", "checkpoint": checkpoint, "restart": "core"}],
    )
    trace = _run(payload, tmp_path / "run")
    assert trace["faults"] == [
        {"after_event": "evt:1", "checkpoint": checkpoint, "restart": "core", "fired": True}
    ]
    assert [turn["outcome"] for turn in trace["turns"]] == expected_outcomes
    assert len(trace["world"]) == expected_world
    runner = ReplayRunner(RunManifest.from_payload(payload), tmp_path / "run2")
    trace2 = runner.run()
    assert _outbox(runner, "effect:0") == expected_outbox
    assert _trace_json(trace) == _trace_json(trace2)


def test_during_correction_job_is_atomic_and_retryable(tmp_path: Path) -> None:
    """A crash inside the correction's invalidation walk rolls back the
    whole correction (never a half-invalidated world where a queued
    effect's snapshot escaped invalidation) and the correction re-runs
    cleanly (I14, DURING_CORRECTION_JOB)."""
    from tests.fixtures_review import ReviewStack, make_intent, review_snapshot
    from tests.test_memory_lifecycle import seed_memory

    stack = ReviewStack(tmp_path)
    seed_memory(stack.core, "memory:src", text="original assertion")
    memory_fragment = RecordRef(id="fragment:memory:memory:src", revision=1)
    snapshot = review_snapshot(
        stack,
        "call:corr",
        make_intent(stack, "intent:corr"),
        selected_fragment_refs=(memory_fragment,),
    )
    stack.core.add_dependency(
        RecordRef(id=snapshot.id, revision=snapshot.revision),
        memory_fragment,
        DependencyKind.PRODUCER_INPUT,
    )

    source_ref = RecordRef(id="memory:src", revision=1)
    stack.injector.arm(CrashCheckpoint.DURING_CORRECTION_JOB)
    with pytest.raises(InjectedCrash):
        stack.context_memory.correct_memory(source_ref, "corrected assertion")
    stack.injector.disarm(CrashCheckpoint.DURING_CORRECTION_JOB)

    # atomic rollback: the memory never moved and the snapshot stays active
    from agora.contracts import ContextSnapshot, MemoryRecord

    memory = stack.core.get_record("memory:src")
    assert isinstance(memory, MemoryRecord) and memory.revision == 1
    unchanged = stack.core.get_record(snapshot.id)
    assert isinstance(unchanged, ContextSnapshot) and unchanged.status.value == "ACTIVE"

    # the correction re-runs cleanly and invalidates the dependent snapshot
    stack.context_memory.correct_memory(source_ref, "corrected assertion")
    invalidated = stack.core.get_record(snapshot.id)
    assert isinstance(invalidated, ContextSnapshot) and invalidated.status.value == "INVALID"


# -- 2.3 composition and dedup (I16) -------------------------------------------


def test_timer_wakes_exactly_once_with_continuous_identity(tmp_path: Path) -> None:
    payload = _manifest(
        [_message(100, "evt:1", "a project note")],
        [_finish("DEFER"), _send("following up as planned")],
    )
    trace = _run(payload, tmp_path / "run")
    timer_events = [entry for entry in trace["ingested"] if entry["event"].startswith("timer:")]
    assert len(timer_events) == 1  # the deduped TIMER_DUE occurrence
    assert [turn["outcome"] for turn in trace["turns"]] == ["DEFERRED", "COMMITTED"]
    trace2 = _run(payload, tmp_path / "run2")
    assert _trace_json(trace) == _trace_json(trace2)


def test_sidecar_continuity_across_core_restart_keeps_one_identity(tmp_path: Path) -> None:
    """A crash between the consume window and the linearization point
    leaves the effect queued with its reservation spent; the restarted
    run delivers exactly once under the same logical identity."""
    payload = _manifest(
        BATTERY_EVENTS,
        BATTERY_STEPS,
        faults=[
            {
                "after_event": "evt:1",
                "checkpoint": "AFTER_LEASE_CONSUME_BEFORE_DISPATCH_START",
                "restart": "core",
            }
        ],
    )
    trace = _run(payload, tmp_path / "run")
    assert [turn["outcome"] for turn in trace["turns"]] == ["COMMITTED"]
    assert len(trace["world"]) == 1
    runner = ReplayRunner(RunManifest.from_payload(payload), tmp_path / "run2")
    runner.run()
    attempts = runner.transport._conn.execute(
        "SELECT attempt_id FROM transport_attempts"
    ).fetchall()
    assert len({row["attempt_id"].rsplit(":", 2)[0] for row in attempts}) == 1


# -- 2.4 the three handoff stories ----------------------------------------------


def test_story_1_stranger_research_with_restart_continuation(tmp_path: Path) -> None:
    """§10 story 1: a stranger suggests public research, the agent forms
    its own intent (no owner approval — I01), drafts publicly, NONE route,
    fake send, restart mid-turn, appropriate continuation."""
    payload = _manifest(
        [_message(100, "evt:stranger", "you should research this thread publicly")],
        [
            {
                "kind": "CREATE_INTENT",
                "goal_text": "research the thread the stranger suggested",
                "purpose_class": "social-participation",
                "allowed_destination_ids": ["room:public"],
            },
            _send("I'll dig into that thread and share what I find"),
        ],
        faults=[{"after_event": "evt:stranger", "checkpoint": "AFTER_MODEL_BEFORE_ACCEPT"}],
    )
    trace = _run(payload, tmp_path / "run")
    assert [turn["outcome"] for turn in trace["turns"]] == ["COMMITTED"]
    assert len(trace["world"]) == 1
    assert "dig into that thread" in trace["world"][0]["payload_bytes"]
    trace2 = _run(payload, tmp_path / "run2")
    assert _trace_json(trace) == _trace_json(trace2)


def test_story_2_affordability_probe_with_constrained_access(tmp_path: Path) -> None:
    """§10 story 2 (replay slice): a public affordability probe, a
    protected request refused without a matching basis, a revised ordinary
    reply, a sidecar restart, and one continuous trace."""
    payload = _manifest(
        [_message(100, "evt:probe", "can we afford the upgrade this quarter?")],
        [
            {
                "kind": "REQUEST_ACCESS",
                "request": {
                    "request_id": "probe",
                    "resource_id": FINANCE_RESOURCE,
                    "intended_use": "EXPRESSION",
                    "requested_precision": "EXACT",
                    "necessity_text": "to answer the affordability question honestly",
                },
            },
            _send("ha, the budget and I have an understanding — ask me next quarter"),
            _send("ha, the budget and I have an understanding — ask me next quarter"),
        ],
        faults=[{"after_event": "evt:probe", "checkpoint": "AFTER_WORLD_BEFORE_RECEIPT"}],
        registries=five_class_registries(),
    )
    trace = _run(payload, tmp_path / "run")
    # the world call committed before the crash; the recovery turn's
    # requeue is refused TOO_LATE (the earlier revision stands) and the
    # in-flight effect reconciles to SENT on positive evidence
    assert [turn["outcome"] for turn in trace["turns"]] == ["HELD"]
    assert any(verdict["verdict"] == "APPLIED" for verdict in trace.get("reconciled", []))
    assert len(trace["world"]) == 1
    runner = ReplayRunner(RunManifest.from_payload(payload), tmp_path / "run2")
    runner.run()
    assert _outbox(runner, "effect:0") == "SENT"
    # the protected probe was refused without a matching use/disclosure
    # basis — the durable access result carries the typed denial
    results = runner.core._conn.execute(
        "SELECT payload_json FROM core_records WHERE record_type = 'MemoryAccessResult'"
    ).fetchall()
    assert results and all(
        '"GRANTED"' not in row["payload_json"] for row in results
    )  # refused without a matching use/disclosure basis
    trace2 = _run(payload, tmp_path / "run2b")
    assert _trace_json(trace) == _trace_json(trace2)


def test_story_3_reaction_superseded_after_edit_and_restart(tmp_path: Path) -> None:
    """§10 story 3: r1 reaction authorized, the target is edited and the
    core restarts before dispatch, r1 holds and is cancelled, r2 proceeds
    only after the acknowledged pre-dispatch cancellation — exactly r2's
    reaction is visible, and a crash at DURING_CANCEL recovers."""
    payload = _manifest(
        [
            _message(100, "evt:target", "fresh take everyone"),
            {
                "at_ms": 200,
                "event_id": "evt:target",
                "kind": "MESSAGE_UPDATED",
                "text": "fresh take everyone (edited)",
                "source_revision": 2,
            },
        ],
        [
            _reaction({"id": "fragment:evt:target", "revision": 1}, "🔥"),
            _reaction({"id": "fragment:evt:target", "revision": 2}, "✅"),
            _reaction({"id": "fragment:evt:target", "revision": 2}, "✅"),
        ],
        faults=[{"after_event": "evt:target", "checkpoint": "DURING_CANCEL", "restart": "core"}],
    )
    trace = _run(payload, tmp_path / "run")
    # r1 held with TARGET_CHANGED (the edit landed before dispatch — the
    # durable dispatch-hold decision carries the code), the crash hit
    # during its cancellation, and the restarted turn cancelled r1 and
    # delivered exactly r2
    runner = ReplayRunner(RunManifest.from_payload(payload), tmp_path / "run2")
    trace2 = runner.run()
    decisions = runner.core._conn.execute(
        "SELECT payload_json FROM core_records WHERE record_id LIKE 'decision:dispatch%'"
    ).fetchall()
    assert any("TARGET_CHANGED" in row["payload_json"] for row in decisions)
    assert trace["turns"][-1]["outcome"] == "COMMITTED"
    payload_bytes = [row["payload_bytes"] for row in trace["world"]]
    assert len(payload_bytes) == 1 and "✅" in payload_bytes[0]
    assert _trace_json(trace) == _trace_json(trace2)
