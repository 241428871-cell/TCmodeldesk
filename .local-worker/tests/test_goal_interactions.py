"""Live goal messages preserve the active objective and durable version boundaries."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import goal_brain_context
import goal_interactions
import goal_requirements
import model_registry as registry
import stage_store
import test_goal_action_receipts as actions
from test_goal_action_receipts import run as run
from test_goal_resolution_http import browser as browser

if TYPE_CHECKING:
    from test_goal_action_receipts import GoalRun
    from test_goal_resolution_http import Browser


def test_btw_is_read_only_and_does_not_claim_or_change_the_goal(run: GoalRun) -> None:
    before = continuation_clock.snapshot(run.scope.project, run.scope.goal_id)
    result = goal_interactions.interact(run.scope.project, run.scope.goal_id, "/btw 现在如何")
    assert result["interaction"] == "progress"
    assert result["progress"]["read_only"] is True
    assert result["progress"]["running_count"] == 0
    assert continuation_clock.snapshot(run.scope.project, run.scope.goal_id) == before
    assert goal_requirements.list_for_goal(run.scope.project, run.scope.goal_id) == []


def test_native_entry_resolves_the_only_active_goal(run: GoalRun) -> None:
    result = goal_interactions.interact_current(run.scope.project, "/btw 从宿主询问")
    assert result["interaction"] == "progress"
    assert result["progress"]["goal_id"] == run.scope.goal_id


def test_btw_prefix_without_a_command_boundary_is_a_requirement(run: GoalRun) -> None:
    result = goal_interactions.interact_current(
        run.scope.project, "/btwlater is ordinary requirement text", request_id="btw-prefix"
    )
    assert result["interaction"] == "requirement"
    assert result["requirement"] is not None
    assert result["requirement"]["version"] == 1


def test_requirement_during_active_round_waits_for_next_safe_round(run: GoalRun) -> None:
    before = continuation_clock.snapshot(run.scope.project, run.scope.goal_id)
    result = goal_interactions.interact(
        run.scope.project,
        run.scope.goal_id,
        "Keep the existing output and add an export action",
        request_id="fixture-live-requirement",
    )
    requirement = result["requirement"]
    assert requirement is not None
    assert requirement["version"] == 1
    assert requirement["status"] == "pending"
    assert requirement["applies_after"] == "current_round"
    after = continuation_clock.snapshot(run.scope.project, run.scope.goal_id)
    assert after["authority_epoch"] == before["authority_epoch"]
    assert after["active_intent_id"] == before["active_intent_id"]
    assert after["requirements_revision"] == 1


def test_only_requirements_seen_by_a_completed_round_are_marked_applied(run: GoalRun) -> None:
    first = goal_requirements.insert(
        run.scope.project, run.scope.goal_id, "First requirement", request_id="first"
    )
    claim = continuation_clock.snapshot(run.scope.project, run.scope.goal_id)["active_intent"]
    assert claim["requirements_revision"] == 0
    _ = continuation_clock.finish_round(
        run.scope.project,
        run.scope.goal_id,
        run.scope.operation_id,
        run.scope.epoch,
        {"status": "completed", "summary": "Original round completed"},
    )
    assert goal_requirements.list_for_goal(run.scope.project, run.scope.goal_id)[0] == first
    next_claim = continuation_clock.claim_due(run.scope.project, run.scope.goal_id, "next-owner")
    assert next_claim is not None
    assert next_claim["requirements_revision"] == 1
    second = goal_requirements.insert(
        run.scope.project, run.scope.goal_id, "Second requirement", request_id="second"
    )
    _ = continuation_clock.finish_round(
        run.scope.project,
        run.scope.goal_id,
        str(next_claim["id"]),
        int(next_claim["authority_epoch"]),
        {"status": "completed", "summary": "Applied first requirement"},
    )
    records = goal_requirements.list_for_goal(run.scope.project, run.scope.goal_id)
    assert records[0]["status"] == "applied"
    assert records[0]["applied_intent_id"] == next_claim["id"]
    assert records[1] == second


def test_context_is_fenced_to_the_claimed_requirement_revision(run: GoalRun) -> None:
    _ = goal_requirements.insert(
        run.scope.project, run.scope.goal_id, "Visible requirement", request_id="visible"
    )
    _ = continuation_clock.finish_round(
        run.scope.project,
        run.scope.goal_id,
        run.scope.operation_id,
        run.scope.epoch,
        {"status": "completed", "summary": "Original round completed"},
    )
    claim = continuation_clock.claim_due(run.scope.project, run.scope.goal_id, "context-owner")
    assert claim is not None
    _ = goal_requirements.insert(
        run.scope.project, run.scope.goal_id, "Later requirement", request_id="later"
    )
    selection = goal_brain_context.select_brain(run.scope.project, run.model_id)
    context = goal_brain_context.build_context(
        run.scope.project,
        run.scope.goal_id,
        selection,
        requirements_through=int(claim["requirements_revision"]),
    )
    assert [item["text"] for item in context["latest_requirements"]] == ["Visible requirement"]


def test_http_btw_and_requirement_use_the_selected_project(
    run: GoalRun, browser: Browser, tmp_path: Path
) -> None:
    path = f"/api/governance/goals/{run.scope.goal_id}/interactions"
    btw = browser.request(path, {"message": "/btw"})
    assert btw.status == 200
    assert btw.body["interaction"] == "progress"
    inserted = browser.request(
        path,
        {
            "message": "Prefer a compact text flow view",
            "kind": "direction_change",
            "request_id": "browser-insert",
        },
    )
    assert inserted.status == 200
    assert inserted.body["requirement"]["kind"] == "direction_change"
    other = tmp_path / "other-project"
    other.mkdir()
    rejected = browser.request(path, {"message": "foreign"}, project=str(other))
    assert rejected.status == 400
    assert len(goal_requirements.list_for_goal(run.scope.project, run.scope.goal_id)) == 1


def test_direction_change_fences_an_older_queued_task_and_records_the_receipt(
    run: GoalRun,
) -> None:
    dispatched = actions.apply(run, actions.proposal(run))
    task_id = dispatched.dispatched_task_ids[0]
    task_run = registry.task_dir(task_id, run.scope.project)
    before = registry.read_task(task_run)
    assert before["status"] == "queued"
    assert before["goal_requirements_revision"] == 0

    result = goal_interactions.interact(
        run.scope.project,
        run.scope.goal_id,
        "Replace the current execution with the new direction",
        kind="direction_change",
        request_id="interrupt-queued-task",
    )

    requirement = result["requirement"]
    assert requirement is not None
    assert requirement["interrupt_state"] == "requested"
    assert requirement["interrupted_task_ids"] == [task_id]
    assert (task_run / "cancelled.flag").is_file()
    current = registry.read_task(task_run)
    assert current["requirements_interrupt"] == {"version": 1, "kind": "direction_change"}
    stage = stage_store.get_stage(str(current["stage_id"]), run.scope.project)
    assert stage is not None
    assert stage["status"] == stage_store.FAILED
    assert stage["data"]["requirements_superseded"]["task_id"] == task_id

    replay = goal_interactions.interact(
        run.scope.project,
        run.scope.goal_id,
        "Replace the current execution with the new direction",
        kind="direction_change",
        request_id="interrupt-queued-task",
    )
    assert replay["requirement"] == requirement


def test_direction_change_exposes_and_repairs_an_incomplete_interrupt_receipt(
    run: GoalRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatched = actions.apply(run, actions.proposal(run))
    task_id = dispatched.dispatched_task_ids[0]
    original = stage_store.mark_requirements_superseded
    attempts = 0

    def fail_once(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise stage_store.StageError("temporary stage receipt failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(stage_store, "mark_requirements_superseded", fail_once)
    first = goal_interactions.interact(
        run.scope.project,
        run.scope.goal_id,
        "Replace the current execution after repairing the receipt",
        kind="direction_change",
        request_id="repair-interrupt-receipt",
    )["requirement"]
    assert first is not None
    assert first["interrupt_state"] == "incomplete"
    assert first["interrupted_task_ids"] == []
    assert first["interrupt_pending_task_ids"] == [task_id]

    replay = goal_interactions.interact(
        run.scope.project,
        run.scope.goal_id,
        "Replace the current execution after repairing the receipt",
        kind="direction_change",
        request_id="repair-interrupt-receipt",
    )["requirement"]
    assert replay is not None
    assert replay["interrupt_state"] == "requested"
    assert replay["interrupted_task_ids"] == [task_id]
    assert replay["interrupt_pending_task_ids"] == []


def test_stop_and_pause_commands_cannot_be_inserted_as_requirements(run: GoalRun) -> None:
    with pytest.raises(goal_interactions.InteractionError, match="目标控制按钮"):
        _ = goal_interactions.interact(run.scope.project, run.scope.goal_id, "/stop now")


def test_requirement_kind_is_checked_at_the_storage_boundary(run: GoalRun) -> None:
    with pytest.raises(goal_requirements.RequirementError, match="需求类型"):
        _ = goal_requirements.insert(
            run.scope.project,
            run.scope.goal_id,
            "invalid kind",
            kind="other",  # type: ignore[arg-type]
        )
