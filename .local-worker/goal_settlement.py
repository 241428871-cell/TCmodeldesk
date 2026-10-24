"""Bind atomic settlement to this installation without adding a second clock."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

import control_bootstrap
from pydantic import JsonValue, TypeAdapter

if TYPE_CHECKING:
    from sqlite3 import Connection

    from model_desk_control.host_flow import HostRef
    from model_desk_control.host_settlement import SettlementRequest

type Record = dict[str, JsonValue]
RECORD: Final = TypeAdapter(Record)


def prepare(scope: HostRef, result: Record) -> SettlementRequest:
    """Parse the actual completion body once, using the original host identity."""
    control_bootstrap.bind_source()
    from model_desk_control.host_settlement import CompletedRound, SettlementRequest

    return SettlementRequest(
        scope=scope, result=CompletedRound.model_validate_json(json.dumps(result))
    )


def from_results(scope: HostRef, actions_json: str, native_json: str) -> SettlementRequest:
    """Derive the completion body exclusively from the saved original phase results."""
    control_bootstrap.bind_source()
    from model_desk_control.host_flow import HostApplied, HostNativeResult
    from model_desk_control.host_settlement import CompletedRound, SettlementRequest

    actions = HostApplied.model_validate_json(actions_json)
    native = HostNativeResult.model_validate_json(native_json)
    return SettlementRequest(
        scope=scope,
        result=CompletedRound(
            status="superseded" if actions.superseded else "completed",
            summary=actions.summary,
            native_session_id=native.native_session_id,
            retryable=True,
            receipt=native.receipt,
            dispatched_task_ids=actions.dispatched_task_ids,
        ),
    )


def payload(request: SettlementRequest) -> Record:
    """Retain the legacy JSON boundary without an unchecked type conversion."""
    return RECORD.validate_json(request.result.model_dump_json())


def verify(
    request: SettlementRequest, project: str, goal_id: str, intent: Record, result: Record
) -> None:
    """An internal settlement argument cannot replace the original intent or outcome."""
    control_bootstrap.bind_source()
    from model_desk_control.host_settlement import verify as verify_request

    verify_request(request, project, goal_id, json.dumps(intent), json.dumps(result))


def commit(database: Connection, request: SettlementRequest) -> None:
    """Leave transaction ownership in the existing continuation repository."""
    control_bootstrap.bind_source()
    from model_desk_control.host_settlement import commit as commit_request

    commit_request(database, request)
