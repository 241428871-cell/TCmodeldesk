"""Versioned dynamic plan contracts independent of any model or host."""

from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from model_desk_control.control import ContractError, ErrorCode

TaskId = Annotated[str, Field(min_length=1, max_length=160)]


class TaskSpec(BaseModel):
    """AI-generated task content; system authority is deliberately absent."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")

    task_id: TaskId
    objective: Annotated[str, Field(min_length=1)]
    acceptance: Annotated[tuple[str, ...], Field(min_length=1)]
    dependencies: tuple[TaskId, ...] = ()


class Plan(BaseModel):
    """Immutable complete revision, including superseded historical tasks."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")

    revision: Annotated[int, Field(ge=0)] = 0
    tasks: tuple[TaskSpec, ...] = ()
    superseded: tuple[TaskId, ...] = ()


class PlanPatch(BaseModel):
    """Ordinary valid replanning requires no human approval flag."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")

    base_revision: Annotated[int, Field(ge=0)]
    reason: Annotated[str, Field(min_length=1)]
    added: tuple[TaskSpec, ...] = ()
    superseded: tuple[TaskId, ...] = ()


def apply_patch(plan: Plan, patch: PlanPatch, *, active_task_ids: frozenset[str]) -> Plan:
    """Validate a graph revision; cancellation of active work is a separate command."""
    if patch.base_revision != plan.revision:
        raise ContractError(ErrorCode.PLAN_REVISION_CONFLICT)
    prior = {task.task_id: task for task in plan.tasks}
    added = {task.task_id: task for task in patch.added}
    if len(added) != len(patch.added) or added.keys() & prior.keys():
        raise ContractError(ErrorCode.DUPLICATE_TASK_IDENTITY)
    if set(patch.superseded) - prior.keys() or set(patch.superseded) & active_task_ids:
        raise ContractError(ErrorCode.CANNOT_REPLACE_UNKNOWN_OR_ACTIVE_TASK)
    superseded = frozenset((*plan.superseded, *patch.superseded))
    tasks = {**prior, **added}
    live = {key: task for key, task in tasks.items() if key not in superseded}
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ContractError(ErrorCode.DEPENDENCY_CYCLE)
        if task_id in visited:
            return
        if task_id not in live:
            raise ContractError(ErrorCode.UNKNOWN_OR_SUPERSEDED_DEPENDENCY)
        visiting.add(task_id)
        for dependency in live[task_id].dependencies:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in live:
        visit(task_id)
    return Plan(
        revision=plan.revision + 1,
        tasks=tuple(tasks.values()),
        superseded=tuple(sorted(superseded)),
    )
