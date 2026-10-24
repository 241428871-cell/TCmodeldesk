"""Plan mutation shared by direct commands and atomic goal-round finalization."""

from sqlite3 import Connection

from model_desk_control.commands import GoalCommand, require_running
from model_desk_control.decision_rules import invalidate_stale
from model_desk_control.plans import Plan, PlanPatch, apply_patch
from model_desk_control.receipts import fingerprint, record, replay
from model_desk_control.task_state import TaskRuntime, TaskStatus, read_plan, read_task, write_task


class PatchCommand(GoalCommand):
    """Only patch content is brain-authored; the adapter provides its trusted header."""

    patch: PlanPatch


def commit_plan_patch(database: Connection, command: PatchCommand) -> Plan:
    """Apply within an existing exclusive transaction, without opening another connection."""
    payload_hash = fingerprint(command.goal_id, f"plan_patch:{command.model_dump_json()}")
    response = replay(database, command.request_id, payload_hash)
    if response is not None:
        return Plan.model_validate_json(response)
    _ = require_running(database, command)
    prior = read_plan(database, command.goal_id)
    states = {
        task.task_id: read_task(database, command.goal_id, task.task_id)
        for task in prior.tasks
        if task.task_id not in prior.superseded
    }
    active = frozenset(
        task_id
        for task_id, state in states.items()
        if state.status in (TaskStatus.DISPATCHING, TaskStatus.RUNNING, TaskStatus.UNKNOWN)
    )
    changed = apply_patch(prior, command.patch, active_task_ids=active)
    _ = database.execute(
        "INSERT INTO plans VALUES (?, ?, ?)",
        (command.goal_id, changed.revision, changed.model_dump_json()),
    )
    for task in command.patch.added:
        _ = database.execute(
            "INSERT INTO task_states VALUES (?, ?, ?)",
            (command.goal_id, task.task_id, TaskRuntime(task_id=task.task_id).model_dump_json()),
        )
    for task_id in command.patch.superseded:
        state = states.get(task_id)
        if state is not None and state.status is not TaskStatus.COMPLETED:
            write_task(
                database,
                command.goal_id,
                state.model_copy(
                    update={"status": TaskStatus.CANCELLED, "revision": state.revision + 1}
                ),
            )
    _ = invalidate_stale(database, command.goal_id)
    record(
        database,
        request_id=command.request_id,
        goal_id=command.goal_id,
        payload_hash=payload_hash,
        response=changed.model_dump_json(),
    )
    return changed
