"""Observed dispatch and context messages extracted from existing native-round receipts."""

from typing import Final

from pydantic import TypeAdapter

from model_desk_control.host_flow import HostApplied
from model_desk_control.host_trace_models import (
    IntentObservation,
    LegacyObservation,
    RecordId,
    RoundMessages,
)
from model_desk_control.native_output import NativeReadError
from model_desk_control.trace_models import MessageEvent
from model_desk_control.trace_projection import MAX_EVENTS

IDS: Final = TypeAdapter(list[RecordId])


class ContextTask(LegacyObservation):
    """A status fed into a completed native round is an observed feedback message."""

    id: RecordId


CONTEXT_TASKS: Final = TypeAdapter(list[ContextTask])


def round_messages(
    project: str,
    intent: IntentObservation,
    receipt: RoundMessages,
) -> list[MessageEvent]:
    """Project only committed sends and status inputs processed by the original native round."""
    events: list[MessageEvent] = []
    dispatched = (
        HostApplied.model_validate_json(receipt.actions).dispatched_task_ids
        if receipt.actions
        else ()
    )
    feedback = CONTEXT_TASKS.validate_json(receipt.context) if receipt.native_done else []
    if len(dispatched) + len(feedback) > MAX_EVENTS:
        code = "round_message_budget_exceeded"
        raise NativeReadError(code)
    for index, task_id in enumerate(dispatched):
        _ = IDS.validate_python([task_id])
        events.append(
            MessageEvent(
                event_id=f"{intent.id}:dispatch:{index}",
                seq=receipt.sequence * 10000 + len(feedback) + index + 1,
                project_id=project,
                goal_id=intent.goal_id,
                source_session="brain:" + intent.id,
                target_session="task:" + task_id,
                message_id=f"{intent.id}:dispatch:{task_id}",
                kind="sent",
                origin="control_dispatch",
            )
        )
    for index, task in enumerate(feedback):
        events.append(
            MessageEvent(
                event_id=f"{intent.id}:context:{index}",
                seq=receipt.sequence * 10000 + index + 1,
                project_id=project,
                goal_id=intent.goal_id,
                source_session="task:" + task.id,
                target_session="brain:" + intent.id,
                message_id=f"{intent.id}:context:{task.id}",
                kind="received",
                origin="context_snapshot",
            )
        )
    return events
