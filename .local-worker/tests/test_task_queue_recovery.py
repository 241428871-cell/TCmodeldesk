"""Recover original unstarted tasks while retaining current authority and launch uncertainty."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pytest

from model_desk_control.host_queue_models import Owner, Phase
from model_desk_control.host_flow import HostRef

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import delegation_policy
import dynamic_plan
import goal_actions
import goal_requirements
import governance_context
import model_registry as registry
import panel_server as panel
import stage_store
import task_queue
import task_queue_driver as driver
from test_goal_action_receipts import GoalRun, apply, proposal
from test_goal_action_receipts import run as run
from test_goal_events import finish_initial
from test_goal_resume import view
from test_task_queue import capture, controls, process

if TYPE_CHECKING:
    from goal_brain_contract import Record
    from pydantic import JsonValue


class RecoveryControls(Protocol):
    def recover_task(self, task_id: str, body: Record) -> Record: ...


def recovery() -> RecoveryControls:
    return panel


def dead_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "INSTANCE", "replacement_runtime")

    def exited(_owner: Owner) -> bool:
        return True

    monkeypatch.setattr(driver, "exited", exited)


def test_owner_exit_requeues_original_task_and_fences_the_old_callback(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    finish_initial(run)
    before = view(run)
    dead_runtime(monkeypatch)
    driver.recover(run.scope.project, run.scope.goal_id, panel.EXECUTOR, panel.execute_task)
    assert len(callbacks) == 2
    launches = process(monkeypatch, run, task_id)
    callbacks[0]()
    assert launches == []
    callbacks[1]()
    assert len(launches) == 1
    assert view(run) == before
    state = registry.read_task(registry.task_dir(task_id))
    receipt = task_queue.observed(state)
    assert receipt is not None
    assert receipt.phase == Phase.DONE
    assert state["id"] == task_id
    assert state["goal_round_id"] == run.scope.operation_id


def test_live_or_unreadable_owner_is_not_proof_of_a_lost_queue(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    _ = apply(run, proposal(run))
    monkeypatch.setattr(driver, "INSTANCE", "new_runtime")

    def still_present(_owner: Owner) -> bool:
        return False

    monkeypatch.setattr(driver, "exited", still_present)
    driver.recover(run.scope.project, run.scope.goal_id, panel.EXECUTOR, panel.execute_task)
    assert len(callbacks) == 1


def test_new_requirement_does_not_requeue_a_task_from_the_old_brain_round(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    finish_initial(run)
    state = registry.read_task(registry.task_dir(task_id))
    before = task_queue.observed(state)
    assert before is not None
    _ = goal_requirements.insert(
        run.scope.project,
        run.scope.goal_id,
        "Replan before starting any task from the previous requirement revision.",
        request_id="queue-fence",
    )
    dead_runtime(monkeypatch)

    driver.recover(run.scope.project, run.scope.goal_id, panel.EXECUTOR, panel.execute_task)

    assert len(callbacks) == 1
    current = registry.read_task(registry.task_dir(task_id))
    after = task_queue.observed(current)
    assert after is not None
    assert after.phase == Phase.CANCELLED
    assert after.ticket == before.ticket
    assert current["status"] == "failed"
    assert current["recovery"]["kind"] == "requirements_superseded"
    old_stage = stage_store.get_stage(str(current["stage_id"]), run.scope.project)
    assert old_stage["status"] == stage_store.FAILED
    assert old_stage["data"]["requirements_superseded"]["task_id"] == task_id

    claim = continuation_clock.claim_due(
        run.scope.project,
        run.scope.goal_id,
        "replacement-brain",
        host_protocol="graph_v2",
    )
    assert claim is not None
    replacement_scope = HostRef(
        project=run.scope.project,
        goal_id=run.scope.goal_id,
        operation_id=str(claim["id"]),
        epoch=int(claim["authority_epoch"]),
        owner_id=str(claim["owner_id"]),
    )
    replacement = proposal(run)
    replacement["plan"] = {
        "base_revision": 1,
        "reason": "Replace the task fenced by the new requirement revision",
        "stages": [{
            **replacement["plan"]["stages"][0],
            "key": "build-after-requirement",
            "title": "Inspect inputs under the latest requirement",
            "objective": "Read current project inputs and the latest requirement",
        }],
    }
    replacement["dispatch_stage_ids"] = ["build-after-requirement"]
    result = goal_actions.apply(
        run.scope.project,
        run.scope.goal_id,
        replacement_scope.epoch,
        replacement,
        panel,
        {},
        str(Path(run.scope.project) / "replacement-actions.json"),
        scope=replacement_scope,
    )

    assert len(result["dispatched_task_ids"]) == 1
    assert result["dispatched_task_ids"][0] != task_id
    stages = dynamic_plan.describe_goal(run.scope.project, run.scope.goal_id)["stages"]
    historical = next(row for row in stages if row["id"] == old_stage["id"])
    successor = next(row for row in stages if row["key"] == "build-after-requirement")
    assert historical["status"] == stage_store.CANCELLED
    assert historical["data"]["dynamic_plan"]["superseded_by"] == 2
    assert successor["status"] == stage_store.DISPATCHED
    replacement_task = registry.read_task(registry.task_dir(result["dispatched_task_ids"][0]))
    assert replacement_task["goal_requirements_revision"] == 1

    _ = continuation_clock.finish_round(
        run.scope.project,
        run.scope.goal_id,
        replacement_scope.operation_id,
        replacement_scope.epoch,
        {"status": "completed", "summary": "Replacement dispatched"},
    )
    replacement_run = registry.task_dir(result["dispatched_task_ids"][0])
    _ = registry.patch_task(
        replacement_run,
        status="completed",
        phase="执行完成，待主脑独立验收",
        ended_at=registry.now(),
    )
    _ = delegation_policy.record_executor(replacement_run, {"completed": True})
    review = delegation_policy.load(replacement_run)
    criteria = [
        {
            "id": item["id"],
            "status": "pass",
            "evidence": [{"source": "reviewer_file", "detail": "Replacement result inspected"}],
        }
        for item in review["criteria"]
    ]
    _ = delegation_policy.accept(replacement_run, "main", criteria)
    _ = stage_store.sync_from_tasks(run.scope.goal_id, project=run.scope.project)
    assert stage_store.get_stage(successor["id"], run.scope.project)["status"] == stage_store.ACCEPTED

    completion_claim = continuation_clock.claim_due(
        run.scope.project,
        run.scope.goal_id,
        "completion-brain",
        host_protocol="graph_v2",
    )
    assert completion_claim is not None
    completion_scope = HostRef(
        project=run.scope.project,
        goal_id=run.scope.goal_id,
        operation_id=str(completion_claim["id"]),
        epoch=int(completion_claim["authority_epoch"]),
        owner_id=str(completion_claim["owner_id"]),
    )
    completed = goal_actions.apply(
        run.scope.project,
        run.scope.goal_id,
        completion_scope.epoch,
        {
            "plan": None,
            "questions": [],
            "dispatch_stage_ids": [],
            "reviews": [],
            "obligations": [],
            "complete": True,
            "summary": "Replacement independently accepted",
        },
        panel,
        {},
        str(Path(run.scope.project) / "completion-actions.json"),
        scope=completion_scope,
    )
    assert completed["goal_completed"] is True
    assert stage_store.get_goal(run.scope.goal_id, run.scope.project)["status"] == stage_store.GOAL_COMPLETED


def test_armed_launch_cannot_be_replayed_even_with_human_confirm_true(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    directory = registry.task_dir(task_id)
    state = registry.read_task(directory)
    receipt = task_queue.observed(state)
    assert receipt is not None
    assert task_queue.begin(state, receipt.delivery_id)
    with governance_context.launch_authority(state):
        task_queue.arm(state, receipt.delivery_id)
    dead_runtime(monkeypatch)
    driver.recover(run.scope.project, run.scope.goal_id, panel.EXECUTOR, panel.execute_task)
    assert len(callbacks) == 1
    _ = controls().recover_pending_tasks([{"run": directory, "project": run.scope.project}])
    assert registry.read_task(directory)["status"] == "recovery_pending"
    with pytest.raises(ValueError, match="启动已发生或无法核对"):
        _ = recovery().recover_task(task_id, {"confirm": True})


def test_changed_prompt_blocks_before_worker_process_creation(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    directory = registry.task_dir(task_id)
    _ = (directory / "prompt.txt").write_text("Replaced after queue admission")
    launches = process(monkeypatch, run, task_id)
    callbacks[0]()
    assert launches == []
    assert registry.read_task(directory)["status"] == "recovery_pending"
    receipt = task_queue.observed(registry.read_task(directory))
    assert receipt is not None
    assert receipt.phase == Phase.BLOCKED


def test_current_stop_blocks_old_queued_callback_and_restart_delivery(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    finish_initial(run)
    _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "stop")
    before = view(run)
    dead_runtime(monkeypatch)
    driver.recover(run.scope.project, run.scope.goal_id, panel.EXECUTOR, panel.execute_task)
    launches = process(monkeypatch, run, task_id)
    callbacks[0]()
    assert launches == []
    assert view(run) == before


@pytest.mark.parametrize("version", [2, True])
def test_unknown_queue_protocol_never_falls_back_to_legacy_execution(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch, *, version: int | bool
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]
    _ = registry.patch_task(registry.task_dir(task_id), queue_protocol=version)
    launches = process(monkeypatch, run, task_id)
    callbacks[0]()
    assert launches == []
    assert registry.read_task(registry.task_dir(task_id))["status"] == "recovery_pending"


def test_positive_no_start_failure_can_resume_the_same_task(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = capture(monkeypatch)
    task_id = apply(run, proposal(run)).dispatched_task_ids[0]

    def fail(*_args: JsonValue, **_kwargs: JsonValue) -> None:
        message = "Fixture Popen refused before creating a process"
        raise OSError(message)

    monkeypatch.setattr("subprocess.Popen", fail)
    callbacks[0]()
    assert registry.read_task(registry.task_dir(task_id))["status"] == "recovery_pending"
    resumed = recovery().recover_task(task_id, {"confirm": True})
    assert resumed["creates_new_record"] is False
    assert len(callbacks) == 2
    launches = process(monkeypatch, run, task_id)
    callbacks[1]()
    assert len(launches) == 1
