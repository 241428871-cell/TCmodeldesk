"""Atomic versioned plans and trusted task observations share the authority database."""

from dataclasses import dataclass

from model_desk_control.plan_commands import PatchCommand, commit_plan_patch
from model_desk_control.plans import Plan
from model_desk_control.store import ControlStore
from model_desk_control.task_state import (
    TaskStatus,
    read_plan,
    read_task,
)

__all__ = ["PatchCommand", "PlanStore"]


@dataclass(frozen=True, slots=True)
class PlanStore:
    """Persist each full revision and its receipt in one transaction."""

    controls: ControlStore

    def read(self, goal_id: str, *, revision: int | None = None) -> Plan:
        """Read the latest plan or a specific immutable historical revision."""
        with self.controls.connection() as database:
            return read_plan(database, goal_id, revision)

    def patch(self, command: PatchCommand) -> Plan:
        """Commit a valid autonomous replan; historical receipts are not authority."""
        with self.controls.connection() as database, database:
            _ = database.execute("BEGIN IMMEDIATE")
            return commit_plan_patch(database, command)

    def ready(self, goal_id: str) -> tuple[str, ...]:
        """Observe ready tasks; this read is not a dispatch authorization."""
        with self.controls.connection() as database:
            _ = database.execute("BEGIN")
            plan = read_plan(database, goal_id)
            states = {
                task.task_id: read_task(database, goal_id, task.task_id) for task in plan.tasks
            }
            return tuple(
                task.task_id
                for task in plan.tasks
                if task.task_id not in plan.superseded
                and states[task.task_id].status is TaskStatus.PLANNED
                and all(
                    states[dependency].status is TaskStatus.COMPLETED
                    for dependency in task.dependencies
                )
            )
