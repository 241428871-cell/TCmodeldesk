# The test/adapter seam used here; the implementation is exercised against temporary real stores.
from contextlib import AbstractContextManager
from sqlite3 import Connection
from typing import TypedDict

from pydantic import JsonValue

from model_desk_control.host_effects import EffectRequest
from model_desk_control.host_initialization_models import InitialDraft

class CreatedGoal(TypedDict):
    id: str

GOAL_ACTIVE: str

def sync_stage(
    stage: dict[str, JsonValue], task: dict[str, JsonValue], record: dict[str, JsonValue],
    goal_status: str | None = None,
) -> str | None: ...

def connection(
    project: str | None = None, create: bool = False
) -> AbstractContextManager[Connection]: ...
def create_goal(
    title: str,
    objective: str,
    project: str | None = None,
    *,
    actor: str = "main",
    store: str = "project",
) -> CreatedGoal: ...
def set_goal_status(
    goal_id: str,
    status: str,
    actor: str = "main",
    project: str | None = None,
    *,
    effect: EffectRequest | None = None,
) -> dict[str, JsonValue]: ...
def get_goal(goal_id: str, project: str | None = None) -> dict[str, JsonValue] | None: ...
def get_stage(stage_id: str, project: str | None = None) -> dict[str, JsonValue] | None: ...
def list_stages(
    goal_id: str | None = None, project: str | None = None
) -> list[dict[str, JsonValue]]: ...
def claim_dispatch(
    stage_id: str, task_id: str, actor: str = "main", mode: str = "auto",
    project: str | None = None,
    *, initialization: InitialDraft | None = None,
) -> dict[str, JsonValue]: ...
def mark_recovery_pending(
    stage_id: str, reason: str, actor: str = "main", project: str | None = None,
) -> dict[str, JsonValue]: ...
def mark_requirements_superseded(
    stage_id: str, task_id: str, reason: str, actor: str = "main",
    project: str | None = None,
) -> dict[str, JsonValue]: ...
def recover(actor: str = "main") -> dict[str, JsonValue]: ...
def requeue_stage(
    stage_id: str, reason: str, actor: str = "main", project: str | None = None,
) -> dict[str, JsonValue]: ...
def sync_from_tasks(
    goal_id: str | None = None, project: str | None = None, *,
    event_intent_id: str = "", event_task_ids: tuple[str, ...] = (),
) -> dict[str, JsonValue]: ...
def sync_project_goal(
    project: str, goal_id: str, *,
    event_intent_id: str = "", event_task_ids: tuple[str, ...] = (),
) -> dict[str, JsonValue]: ...
