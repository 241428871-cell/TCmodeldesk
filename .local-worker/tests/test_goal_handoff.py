"""Owner changes must fence delayed actions without resuming paused work."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from model_desk_control.database import text_cursor
from model_desk_control.host_handoff import HandoffError, HandoffReceipt
from pydantic import JsonValue, TypeAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock as clock
import continuation_store as store
import goal_controller
import model_registry as registry
import workspace_config
from test_goal_action_receipts import GoalRun, apply, proposal
from test_goal_action_receipts import run as run  # noqa: PLC0414 - re-exported pytest fixture

if TYPE_CHECKING:
    from goal_brain_contract import Record


def saved(run: GoalRun) -> Record:
    with store.readonly(run.scope.project) as database:
        assert database is not None
        row = text_cursor(
            database, "SELECT data FROM continuation_clocks WHERE goal_id=?", (run.scope.goal_id,)
        ).fetchone()
        assert row is not None
        return TypeAdapter(dict[str, JsonValue]).validate_json(row[0])


def paused(run: GoalRun) -> Record:
    _ = clock.record_runner(
        run.scope.project,
        run.scope.goal_id,
        run.scope.operation_id,
        run.scope.epoch,
        900001,
        1234.0,
        "original-session",
    )
    _ = clock.finish_round(
        run.scope.project,
        run.scope.goal_id,
        run.scope.operation_id,
        run.scope.epoch,
        {"status": "completed"},
    )
    _ = clock.control(run.scope.project, run.scope.goal_id, "pause")
    return saved(run)


def request(run: GoalRun, identity: str = "transfer-one") -> Record:
    state = saved(run)
    return {
        "request_id": identity,
        "model_id": run.model_id,
        "expected_owner_epoch": state.get("owner_epoch", 0),
        "expected_authority_epoch": state["authority_epoch"],
    }


def transfer(run: GoalRun, body: Record) -> HandoffReceipt:
    return HandoffReceipt.model_validate(
        goal_controller.control_goal(run.scope.project, run.scope.goal_id, "handoff", body)
    )


def test_transfer_archives_identity_preserves_work_and_does_not_resume(run: GoalRun) -> None:
    before = paused(run)
    receipt = transfer(run, request(run))
    after = saved(run)
    assert receipt.previous_native_session_id == "original-session"
    assert receipt.owner_epoch == 1
    assert after["native_session_id"] == ""
    for field in ("user_intent", "enabled", "obligations", "interval_seconds", "next_due_at"):
        assert after[field] == before[field]
    assert clock.claim_due(run.scope.project, run.scope.goal_id, "new-owner") is None
    assert not clock.dispatch_guard(
        run.scope.project, run.scope.goal_id, authority_epoch=run.scope.epoch
    )["allowed"]
    with pytest.raises(ValueError, match="旧轮次"):
        _ = clock.record_runner(
            run.scope.project,
            run.scope.goal_id,
            run.scope.operation_id,
            run.scope.epoch,
            900001,
            1234.0,
        )


def test_replay_is_identical_but_conflicting_payload_cannot_overwrite(run: GoalRun) -> None:
    _ = paused(run)
    body = request(run)
    first = transfer(run, body)
    assert transfer(run, body) == first
    with pytest.raises(HandoffError, match="request_conflict"):
        _ = transfer(run, {**body, "model_id": "another-model"})
    history = saved(run)["owner_history"]
    assert isinstance(history, list)
    assert len(history) == 1
    assert HandoffReceipt.model_validate(history[0]) == first


@pytest.mark.parametrize("action", ["running", "pause", "stop"])
def test_live_or_revoked_round_cannot_be_handed_off(run: GoalRun, action: str) -> None:
    if action != "running":
        _ = clock.control(run.scope.project, run.scope.goal_id, action)
    with pytest.raises(HandoffError, match=r"requires_unheld_pause|unsettled_round"):
        _ = transfer(run, request(run))


def test_concurrent_handoffs_have_only_one_winner(run: GoalRun) -> None:
    _ = paused(run)
    bodies = [request(run, "first"), request(run, "second")]

    def attempt(body: Record) -> str:
        try:
            _ = transfer(run, body)
        except HandoffError as error:
            return error.code
        return "transferred"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, bodies))
    assert sorted(results) == ["handoff_stale_epoch", "transferred"]
    assert saved(run)["owner_epoch"] == 1


def test_model_validation_failure_rolls_back_owner_and_history(run: GoalRun) -> None:
    before = paused(run)
    with pytest.raises(ValueError, match="模型"):
        _ = transfer(run, {**request(run), "model_id": "missing-model"})
    assert saved(run) == before


def test_queued_worker_blocks_handoff_even_after_round_finishes(run: GoalRun) -> None:
    _ = apply(run, proposal(run))
    _ = paused(run)
    with pytest.raises(HandoffError, match="unsettled_worker"):
        _ = transfer(run, request(run))


def test_new_round_carries_new_owner_generation(run: GoalRun) -> None:
    _ = paused(run)
    receipt = transfer(run, request(run))
    resumed = clock.control(run.scope.project, run.scope.goal_id, "resume")
    _ = clock.control(run.scope.project, run.scope.goal_id, "run_now")
    claim = clock.claim_due(run.scope.project, run.scope.goal_id, "new-owner")
    assert claim is not None
    with store.readonly(run.scope.project) as database:
        assert database is not None
        row = text_cursor(
            database, "SELECT data FROM continuation_intents WHERE id=?", (claim["id"],)
        ).fetchone()
        assert row is not None
        record = TypeAdapter(dict[str, JsonValue]).validate_json(row[0])
    assert record["owner_epoch"] == receipt.owner_epoch
    assert record["authority_epoch"] == resumed["authority_epoch"]
    assert record["native_session_id"] == ""


def test_codex_to_claude_and_back_uses_project_bindings(run: GoalRun) -> None:
    _ = paused(run)
    model = registry.upsert_model(
        {"provider": "claude_cli", "name": "Other brain", "model": "fixture"}
    )
    model_id = model["id"]
    assert isinstance(model_id, str)
    _ = workspace_config.update_binding(
        {"project": run.scope.project, "model_id": model_id, "role": "decision"}
    )
    first = transfer(run, {**request(run), "model_id": model_id})
    assert saved(run)["brain_model_id"] == model_id
    second = transfer(run, request(run, "back-to-codex"))
    assert second.previous_model_id == model_id
    assert second.owner_epoch == first.owner_epoch + 1
    assert saved(run)["brain_model_id"] == run.model_id


def test_orphan_revoked_receipt_blocks_transfer_without_active_pointer(run: GoalRun) -> None:
    _ = clock.control(run.scope.project, run.scope.goal_id, "pause")
    state = saved(run)
    with store.mutation(run.scope.project, run.scope.goal_id) as database:
        store.write(database, {**state, "active_intent_id": ""})
    with pytest.raises(HandoffError, match="unsettled_round"):
        _ = transfer(run, request(run))


def test_old_action_cannot_dispatch_after_handoff(run: GoalRun) -> None:
    old_proposal = proposal(run)
    _ = paused(run)
    _ = transfer(run, request(run))
    _ = clock.control(run.scope.project, run.scope.goal_id, "resume")
    with pytest.raises(ValueError, match="授权代次"):
        _ = apply(run, old_proposal)
    assert run.queued == []


def test_owner_fence_rejects_tampered_generation_even_with_current_authority(run: GoalRun) -> None:
    state = saved(run)
    with store.mutation(run.scope.project, run.scope.goal_id) as database:
        store.write(database, {**state, "owner_epoch": 1})
    with pytest.raises(ValueError, match="旧轮次"):
        _ = clock.record_runner(
            run.scope.project,
            run.scope.goal_id,
            run.scope.operation_id,
            run.scope.epoch,
            900001,
            1234.0,
        )


def test_same_request_replay_cannot_roll_back_a_later_owner(run: GoalRun) -> None:
    _ = paused(run)
    body = request(run)
    first = transfer(run, body)
    _ = transfer(run, request(run, "next-transfer"))
    before = saved(run)
    assert transfer(run, body) == first
    assert saved(run) == before
