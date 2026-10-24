"""Bind source events to the existing project database, clock and native round driver."""

import json
import logging
from datetime import UTC, datetime
from sqlite3 import Connection
from sqlite3 import Error as DatabaseError

import continuation_store
import control_bootstrap
import stage_store
from pydantic import JsonValue, TypeAdapter


def answered(database: Connection, project: str, goal_id: str, decision_id: str) -> None:
    """Called inside the original answer transaction, with no model or service startup."""
    control_bootstrap.bind_source()
    from model_desk_control.host_event_sources import answered as record

    record(database, project, goal_id, decision_id)


def task_changed(
    database: Connection,
    stage_id: str,
    task: dict[str, JsonValue] | None,
    review: dict[str, JsonValue] | None,
    *,
    caused_by: str = "",
) -> None:
    """Legacy tasks keep periodic behavior; managed tasks retain their original round."""
    if not task or not task.get("goal_round_id"):
        return
    control_bootstrap.bind_source()
    from model_desk_control.host_event_sources import task_changed as record

    record(database, stage_id, json.dumps(task), json.dumps(review or {}), caused_by=caused_by)


def ready(project: str, goal_id: str, epoch: int) -> bool:
    """The existing scanner reads a bounded inbox; it does not poll the model."""
    control_bootstrap.bind_source()
    from model_desk_control.host_events import clock, pending

    with continuation_store.readonly(project) as database:
        if database is None:
            return False
        state = clock(database, project, goal_id)
        return (
            state is not None
            and (state.event_not_before is None or state.event_not_before <= datetime.now(UTC))
            and bool(pending(database, project, goal_id, epoch))
        )


def claim(
    database: Connection, project: str, goal_id: str, epoch: int, intent_id: str
) -> list[str]:
    """Called only inside the original claim_due transaction after current clock guards."""
    control_bootstrap.bind_source()
    from model_desk_control.host_events import bind

    return bind(database, project, goal_id, epoch, intent_id)


def sync_task(state: dict[str, JsonValue], *, caused_by: str = "") -> None:
    """Completion callbacks observe saved results; they never change a task's result."""
    if not state.get("goal_round_id"):
        return
    control_bootstrap.bind_source()
    from model_desk_control.host_trace_models import TaskObservation

    observed = TaskObservation.model_validate_json(json.dumps(state))
    if not observed.goal_id or not observed.stage_id:
        return
    try:
        _ = stage_store.sync_project_goal(
            observed.project,
            observed.goal_id,
            event_intent_id=caused_by,
            event_task_ids=(observed.id,),
        )
    except (ValueError, OSError, DatabaseError):
        logging.getLogger(__name__).exception(
            "Task event needs reconciliation; original result retained: %s", observed.id
        )


def context(project: str, goal_id: str, claim: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Explain the durable trigger while preserving the native main-brain interface."""
    control_bootstrap.bind_source()
    from model_desk_control.host_event_context import context as read
    from model_desk_control.host_events import EventError

    with continuation_store.readonly(project) as database:
        if database is None:
            code = "event_context_project_missing"
            raise EventError(code)
        result = read(database, project, goal_id, json.dumps(claim))
        return TypeAdapter(dict[str, JsonValue]).validate_json(result.model_dump_json())
