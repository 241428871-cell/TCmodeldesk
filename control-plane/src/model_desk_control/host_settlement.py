"""Commit a graph settlement marker inside the original round's SQL transaction."""

from hashlib import sha256
from sqlite3 import Connection
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from model_desk_control.database import text_cursor
from model_desk_control.host_flow import HostRecord, HostRef


class SettlementError(ValueError):
    """An original round cannot be settled using unrelated or changed evidence."""

    def __init__(self, code: str) -> None:
        """Expose a stable reason without copying provider output into the error."""
        self.code: str = code
        super().__init__(code)


class CompletedRound(HostRecord):
    """The exact original summary, without a mutable current-clock snapshot."""

    status: Literal["completed", "superseded"]
    summary: str
    native_session_id: str
    retryable: Literal[True]
    receipt: str
    dispatched_task_ids: tuple[str, ...]


class SettlementReceipt(HostRecord):
    """A compact historical result must never be mistaken for current authorization."""

    operation_id: str
    epoch: int
    result_digest: str


class SettlementRequest(HostRecord):
    """Only the host supplies the original scope and validated completion result."""

    scope: HostRef
    result: CompletedRound

    def receipt(self) -> str:
        """Keep the saved graph result stable across subsequent clock changes."""
        return SettlementReceipt(
            operation_id=self.scope.operation_id,
            epoch=self.scope.epoch,
            result_digest=sha256(self.result.model_dump_json().encode()).hexdigest(),
        ).model_dump_json()


class IntentOwner(BaseModel):
    """Inspect only the immutable original intent identity at the legacy boundary."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    id: str
    goal_id: str
    authority_epoch: int
    owner_id: str


def verify(
    request: SettlementRequest, project: str, goal_id: str, intent_json: str, result_json: str
) -> None:
    """The original intent and actual result must match before any domain write."""
    owner = IntentOwner.model_validate_json(intent_json)
    actual = HostRef(
        project=project,
        goal_id=owner.goal_id,
        operation_id=owner.id,
        epoch=owner.authority_epoch,
        owner_id=owner.owner_id,
    )
    if (
        goal_id != owner.goal_id
        or actual != request.scope
        or CompletedRound.model_validate_json(result_json) != request.result
    ):
        code = "settlement_scope_or_result_conflict"
        raise SettlementError(code)


def commit(database: Connection, request: SettlementRequest) -> None:
    """The caller commits or rolls back this marker together with the original round."""
    identity = sha256(request.scope.model_dump_json().encode()).hexdigest()
    row = text_cursor(
        database,
        """SELECT CAST(version AS TEXT),status,COALESCE(result_json,'')
           FROM goal_graph_steps WHERE intent_id=? AND step='settle' AND scope_hash=?""",
        (request.scope.operation_id, identity),
    ).fetchone()
    if row is None or row[0] != "2":
        code = "settlement_phase_not_prepared"
        raise SettlementError(code)
    receipt = request.receipt()
    if row[1] == "done":
        if row[2] != receipt:
            code = "settlement_completed_receipt_conflict"
            raise SettlementError(code)
        return
    cursor = database.execute(
        """UPDATE goal_graph_steps SET status='done',result_json=?
           WHERE intent_id=? AND step='settle' AND scope_hash=?
           AND version=2 AND status='inflight'""",
        (receipt, request.scope.operation_id, identity),
    )
    if cursor.rowcount != 1:
        code = "settlement_phase_commit_conflict"
        raise SettlementError(code)
