"""Explicit owner transfer in the original goal transaction, without dispatch."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from sqlite3 import Connection
from typing import Annotated, ClassVar, Final

from pydantic import ConfigDict, Field, JsonValue, TypeAdapter

from model_desk_control.database import text_cursor
from model_desk_control.host_events import exists
from model_desk_control.host_flow import HostRecord

RECORD: Final = TypeAdapter(dict[str, JsonValue])


class HandoffError(ValueError):
    """A transfer cannot establish exclusive, current ownership."""

    def __init__(self, code: str) -> None:
        """Expose a stable refusal reason without private native output."""
        self.code: str = code
        super().__init__(code)


class HandoffRequest(HostRecord):
    """A replay identity and both observed generations are required."""

    request_id: Annotated[str, Field(min_length=1, max_length=100)]
    model_id: Annotated[str, Field(min_length=1, max_length=300)]
    expected_owner_epoch: Annotated[int, Field(ge=0)]
    expected_authority_epoch: Annotated[int, Field(ge=1)]


class HandoffReceipt(HostRecord):
    """The prior native identity remains archival, never a new resume target."""

    request: HandoffRequest
    previous_model_id: str
    previous_native_session_id: str
    owner_epoch: int
    authority_epoch: int
    at: str


class OwnerState(HostRecord):
    """Read old clocks with owner generation zero while preserving other fields."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    goal_id: str
    project: str
    enabled: bool
    user_intent: str
    closed: bool
    active_intent_id: str
    hold: dict[str, JsonValue]
    authority_epoch: int
    owner_epoch: int = 0
    brain_model_id: str
    native_session_id: str
    owner_history: list[HandoffReceipt] = Field(default_factory=list)


def _quiescent(database: Connection, state: OwnerState) -> None:
    if state.enabled or state.user_intent != "USER_PAUSE" or state.closed or state.hold:
        code = "handoff_requires_unheld_pause"
        raise HandoffError(code)
    live = text_cursor(
        database,
        """SELECT id FROM continuation_intents WHERE goal_id=?
        AND status IN ('claimed','started','unknown','revoked') LIMIT 1""",
        (state.goal_id,),
    ).fetchone()
    if state.active_intent_id or live is not None:
        code = "handoff_unsettled_round"
        raise HandoffError(code)
    stage = text_cursor(
        database,
        "SELECT id FROM stages WHERE goal_id=? AND status IN ('dispatched','running') LIMIT 1",
        (state.goal_id,),
    ).fetchone()
    if stage is not None:
        code = "handoff_unsettled_worker"
        raise HandoffError(code)
    if exists(database, "goal_task_queue"):
        queued = text_cursor(
            database,
            """SELECT task_id FROM goal_task_queue WHERE goal_id=?
            AND phase NOT IN ('done','blocked','cancelled') LIMIT 1""",
            (state.goal_id,),
        ).fetchone()
        if queued is not None:
            code = "handoff_unsettled_worker"
            raise HandoffError(code)


def transfer(
    database: Connection,
    project: str,
    goal_id: str,
    request: HandoffRequest,
    validate_model: Callable[[], None],
) -> HandoffReceipt:
    """CAS, identity archive and epoch fences commit together in the caller's lock."""
    if not database.in_transaction:
        code = "handoff_transaction_required"
        raise HandoffError(code)
    row = text_cursor(
        database, "SELECT data FROM continuation_clocks WHERE goal_id=?", (goal_id,)
    ).fetchone()
    if row is None:
        code = "handoff_clock_missing"
        raise HandoffError(code)
    raw = RECORD.validate_json(row[0])
    state = OwnerState.model_validate(raw)
    if (state.project, state.goal_id) != (project, goal_id):
        code = "handoff_project_mismatch"
        raise HandoffError(code)
    for previous in state.owner_history:
        if previous.request.request_id == request.request_id:
            if previous.request != request:
                code = "handoff_request_conflict"
                raise HandoffError(code)
            return previous
    if (state.owner_epoch, state.authority_epoch) != (
        request.expected_owner_epoch,
        request.expected_authority_epoch,
    ):
        code = "handoff_stale_epoch"
        raise HandoffError(code)
    goal = text_cursor(
        database, "SELECT project,status FROM goals WHERE id=?", (goal_id,)
    ).fetchone()
    if goal is None or tuple(goal) != (project, "active"):
        code = "handoff_goal_not_active"
        raise HandoffError(code)
    _quiescent(database, state)
    validate_model()
    receipt = HandoffReceipt(
        request=request,
        previous_model_id=state.brain_model_id,
        previous_native_session_id=state.native_session_id,
        owner_epoch=state.owner_epoch + 1,
        authority_epoch=state.authority_epoch + 1,
        at=datetime.now(UTC).isoformat(),
    )
    raw.update(
        owner_epoch=receipt.owner_epoch,
        authority_epoch=receipt.authority_epoch,
        brain_model_id=request.model_id,
        native_session_id="",
        updated_at=receipt.at,
        owner_history=[
            RECORD.validate_json(item.model_dump_json()) for item in [*state.owner_history, receipt]
        ],
    )
    _ = database.execute(
        "UPDATE continuation_clocks SET data=? WHERE goal_id=?",
        (json.dumps(raw, ensure_ascii=False), goal_id),
    )
    return receipt
