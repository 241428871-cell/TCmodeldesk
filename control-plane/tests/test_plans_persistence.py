"""Versioned plans use real authority and observations under competing connections."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from model_desk_control.control import ContractError
from model_desk_control.database import StoreError
from model_desk_control.plan_store import PatchCommand, PlanStore
from model_desk_control.plans import PlanPatch
from model_desk_control.store import ControlStore
from model_desk_control.task_state import TaskStatus, read_task, write_task
from tests.domain_fixtures import domain_at, task


def add_command(request_id: str) -> PatchCommand:
    return PatchCommand(
        goal_id="project/goal",
        request_id=request_id,
        epoch=0,
        control_revision=0,
        patch=PlanPatch(
            base_revision=1, reason="Add an independent task", added=(task(request_id),)
        ),
    )


def test_history_and_receipt_survive_reopen(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    plans = PlanStore(controls)
    command = add_command("d")
    changed = plans.patch(command)
    reopened = PlanStore(ControlStore(controls.path))
    assert reopened.read("project/goal") == changed
    assert reopened.patch(command) == changed
    assert reopened.read("project/goal", revision=1).revision == 1
    assert reopened.read("project/goal", revision=0).tasks == ()
    with pytest.raises(StoreError, match="request_id_payload_conflict"):
        _ = reopened.patch(
            command.model_copy(update={"patch": PlanPatch(base_revision=2, reason="Changed body")})
        )


def test_two_replans_compete_for_one_revision(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)

    def compete(identity: str) -> str:
        try:
            _ = PlanStore(ControlStore(controls.path)).patch(add_command(identity))
        except ContractError as error:
            return error.code
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(compete, ("d", "e")))
    assert sorted(outcomes) == ["committed", "plan_revision_conflict"]
    assert PlanStore(controls).read("project/goal").revision == 2


def test_invalid_graph_rolls_back_plan_task_rows_and_receipt(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    plans = PlanStore(controls)
    invalid = add_command("d").model_copy(
        update={
            "patch": PlanPatch(
                base_revision=1,
                reason="Invalid dependency",
                added=(task("d", dependencies=("x",)),),
            )
        }
    )
    with pytest.raises(ContractError, match="unknown_or_superseded_dependency"):
        _ = plans.patch(invalid)
    assert plans.read("project/goal").revision == 1
    # Reusing the request ID with a corrected body succeeds because no receipt was committed.
    assert plans.patch(add_command("d")).revision == 2


@pytest.mark.parametrize("status", [TaskStatus.DISPATCHING, TaskStatus.RUNNING, TaskStatus.UNKNOWN])
def test_model_cannot_supersede_active_database_observation(
    tmp_path: Path, status: TaskStatus
) -> None:
    controls = domain_at(tmp_path)
    with controls.connection() as database, database:
        state = read_task(database, "project/goal", "a")
        write_task(database, "project/goal", state.model_copy(update={"status": status}))
    command = add_command("replace").model_copy(
        update={
            "patch": PlanPatch(base_revision=1, reason="Replace active task", superseded=("a", "c"))
        }
    )
    with pytest.raises(ContractError, match="cannot_replace_unknown_or_active_task"):
        _ = PlanStore(controls).patch(command)


def test_stop_fences_new_plan_but_keeps_history(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    plans = PlanStore(controls)
    receipt = plans.patch(add_command("d"))
    _ = controls.stop(goal_id="project/goal", request_id="stop")
    assert plans.patch(add_command("d")) == receipt
    with pytest.raises(ContractError, match="stale_authority_or_revision"):
        _ = plans.patch(add_command("e"))
    assert plans.read("project/goal").revision == 2


def test_missing_task_observation_is_never_assumed_idle(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    with controls.connection() as database, database:
        _ = database.execute("DELETE FROM task_states WHERE task_id = 'a'")
    with pytest.raises(StoreError, match="task_state_not_found"):
        _ = PlanStore(controls).patch(add_command("d"))
