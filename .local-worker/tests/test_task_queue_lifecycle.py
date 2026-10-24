"""Cancellation, incomplete creation and invalid input cannot silently restart model work."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pytest

from model_desk_control.database import text_cursor
from model_desk_control.host_queue_store import candidates

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import governance_context
import model_registry as registry
import panel_server as panel
import stage_store
import task_queue
import task_queue_driver as driver
from test_goal_action_receipts import GoalRun, apply, proposal
from test_goal_action_receipts import run as run
from test_task_queue import capture, controls, process
from test_task_queue_recovery import dead_runtime

if TYPE_CHECKING:
    from goal_brain_contract import Record


class Cancellation(Protocol):
    def cancel_task(self, task_id: str) -> Record: ...


def canceller() -> Cancellation:
    return panel


def test_cancelled_queue_is_retired_without_starting_the_old_callback(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    _ = canceller().cancel_task(task_id)
    launches = process(monkeypatch, run, task_id)
    callbacks[0]()
    receipt = task_queue.observed(registry.read_task(registry.task_dir(task_id)))
    assert receipt is not None
    assert receipt.phase.value == "cancelled"
    assert launches == []


def test_invalid_saved_input_is_quarantined_from_recovery_scanning(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    directory = registry.task_dir(task_id)
    _ = (directory / "prompt.txt").write_text("Changed after admission")
    dead_runtime(monkeypatch)
    driver.recover(run.scope.project, run.scope.goal_id, panel.EXECUTOR, panel.execute_task)
    assert len(callbacks) == 1
    state = registry.read_task(directory)
    assert state["status"] == "recovery_pending"
    with stage_store.connection(run.scope.project) as database:
        assert candidates(database, run.scope.goal_id) == ()
    launches = process(monkeypatch, run, task_id)
    callbacks[0]()
    assert launches == []


def test_partial_task_initialization_is_not_a_completed_dispatch_receipt(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_before_receipt(_state: Record) -> None:
        message = "Fixture task metadata exists but durable preparation failed"
        raise OSError(message)

    callbacks = capture(monkeypatch)
    monkeypatch.setattr(task_queue, "prepare", fail_before_receipt)
    with pytest.raises(ValueError, match="变更结果未知"):
        _ = apply(run, proposal(run))
    assert callbacks == []
    with pytest.raises(ValueError, match="initialization_io_incomplete"):
        _ = apply(run, proposal(run))
    assert callbacks == []


def test_restart_retains_an_exact_live_launcher_and_its_original_stage(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    directory = registry.task_dir(task_id)
    state = registry.read_task(directory)
    receipt = task_queue.observed(state)
    assert receipt is not None
    assert task_queue.begin(state, receipt.delivery_id)
    with governance_context.launch_authority(state):
        task_queue.arm(state, receipt.delivery_id)
        task_queue.started(state, receipt.delivery_id, os.getpid())
    _ = registry.patch_task(directory, status="running")
    _ = controls().recover_pending_tasks([{"run": directory, "project": run.scope.project}])
    _ = stage_store.recover()
    assert registry.read_task(directory)["status"] == "running"
    with stage_store.connection(run.scope.project) as database:
        row = text_cursor(
            database, "SELECT status FROM stages WHERE active_task_id=?", (task_id,)
        ).fetchone()
    assert row is not None
    assert row[0] == "dispatched"


@pytest.mark.parametrize("damage", ["invalid_json", "missing_protocol"])
def test_invalid_callback_keeps_a_durable_recovery_reason(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    directory = registry.task_dir(task_id)
    if damage == "invalid_json":
        _ = (directory / "task.json").write_text("{")
    else:
        _ = registry.patch_task(directory, queue_protocol=None)
    launches = process(monkeypatch, run, task_id)
    callbacks[0]()
    assert launches == []
    receipt = task_queue.observed({"project": run.scope.project, "id": task_id})
    assert receipt is not None
    assert receipt.phase.value == "blocked"
    assert receipt.reason == "queue_callback_input_invalid"
