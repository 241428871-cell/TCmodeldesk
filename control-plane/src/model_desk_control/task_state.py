"""Persisted task observations and stable context hashes for relevant decisions."""

from enum import StrEnum
from hashlib import sha256
from sqlite3 import Connection
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from model_desk_control.commands import Revision
from model_desk_control.database import StoreError, text_cursor
from model_desk_control.plans import Plan, TaskId, TaskSpec

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class TaskStatus(StrEnum):
    """Unknown native work remains active until an independent reconciliation."""

    PLANNED = "planned"
    WAITING = "waiting"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    UNKNOWN = "unknown"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskRuntime(BaseModel):
    """Trusted observations; AI plan patches cannot set these fields."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")
    task_id: TaskId
    revision: Revision = 0
    context_revision: Revision = 0
    status: TaskStatus = TaskStatus.PLANNED
    policy_revision: Revision = 0
    artifact_hash: Digest | None = None


def read_plan(database: Connection, goal_id: str, revision: int | None = None) -> Plan:
    """Read immutable history or the latest revision without initializing missing plans."""
    statement = """SELECT CAST(revision AS TEXT), document FROM plans
                   WHERE goal_id = ? ORDER BY revision DESC LIMIT 1"""
    parameters = (goal_id,)
    if revision is not None:
        statement = """SELECT CAST(revision AS TEXT), document FROM plans
                       WHERE goal_id = ? AND revision = ?"""
        parameters = (goal_id, str(revision))
    row = text_cursor(database, statement, parameters).fetchone()
    if row is None:
        code = "plan_not_initialized" if revision is None else "plan_revision_not_found"
        raise StoreError(code)
    plan = Plan.model_validate_json(row[1])
    if plan.revision != int(row[0]):
        code = "plan_record_revision_mismatch"
        raise StoreError(code)
    return plan


def read_task(database: Connection, goal_id: str, task_id: str) -> TaskRuntime:
    """Read a trusted task state; missing observations cannot be assumed idle."""
    row = text_cursor(
        database,
        "SELECT document FROM task_states WHERE goal_id = ? AND task_id = ?",
        (goal_id, task_id),
    ).fetchone()
    if row is None:
        code = "task_state_not_found"
        raise StoreError(code)
    state = TaskRuntime.model_validate_json(row[0])
    if state.task_id != task_id:
        code = "task_record_identity_mismatch"
        raise StoreError(code)
    return state


def write_task(database: Connection, goal_id: str, state: TaskRuntime) -> None:
    """Update an existing observation inside the caller's exclusive transaction."""
    cursor = database.execute(
        "UPDATE task_states SET document = ? WHERE goal_id = ? AND task_id = ?",
        (state.model_dump_json(), goal_id, state.task_id),
    )
    if cursor.rowcount != 1:
        code = "task_state_not_found"
        raise StoreError(code)


def live_task(plan: Plan, task_id: str) -> TaskSpec:
    """Reject superseded subjects even though their immutable history remains readable."""
    for task in plan.tasks:
        if task.task_id == task_id and task_id not in plan.superseded:
            return task
    code = "task_not_live"
    raise StoreError(code)


def task_digest(task: TaskSpec) -> str:
    """Bind only relevant task content, so unrelated new tasks do not invalidate answers."""
    return sha256(task.model_dump_json().encode()).hexdigest()
