"""Inventory and purge every durable goal/task record in one original project store."""

from hashlib import sha256
from sqlite3 import Connection
from typing import Annotated, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from model_desk_control.database import text_cursor
from model_desk_control.host_events import exists
from model_desk_control.host_flow import HostRecord, HostRef

MAX_ROWS: Final = 20_000
ACTIVE_INTENTS: Final = frozenset({"claimed", "started", "unknown"})
OPEN_REQUESTS: Final = frozenset({"reserved", "unknown"})
OPEN_QUEUE: Final = frozenset({"ready", "queued", "claimed", "armed", "started", "unknown"})
# Goal-keyed tables, including private prompt copies held by initialization manifests.
GOAL_TABLES: Final = (
    "goal_events",
    "goal_task_branches",
    "goal_task_initializations",
    "goal_task_queue",
    "human_decisions",
    "plan_revisions",
    "goal_contracts",
    "goal_requirements",
    "continuation_clocks",
    "continuation_intents",
)
TASK_TABLES: Final = (
    "goal_task_queue",
    "goal_task_initializations",
    "human_decision_uses",
    "brain_requests",
)

type Row = tuple[str, ...]


class PurgeError(ValueError):
    """The original records are unreadable or changed during an authorized purge."""

    def __init__(self, code: str) -> None:
        """Retain a stable machine reason without record contents."""
        self.code: str = code
        super().__init__(code)


class PurgeScope(HostRecord):
    """Finished goals and terminal tasks selected by the existing preview."""

    project: Annotated[str, Field(min_length=1)]
    goal_ids: tuple[str, ...]
    task_ids: tuple[str, ...]


class _Intent(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    authority_epoch: int
    owner_id: str


class _Obligation(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    status: str


class _Clock(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    active_intent_id: str = ""
    hold: dict[str, JsonValue] = {}
    obligations: tuple[_Obligation, ...] = ()


class PurgeInventory(HostRecord):
    """Identities to remove and the reasons that keep them; no prompt or answer text."""

    intent_ids: tuple[str, ...]
    effect_owners: tuple[str, ...]
    goal_threads: tuple[str, ...]
    task_threads: tuple[str, ...]
    counts: tuple[tuple[str, int], ...]
    blockers: tuple[str, ...]
    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def _marks(count: int) -> str:
    return ",".join("?" for _ in range(count))


def _rows(
    database: Connection, table: str, column: str, keys: tuple[str, ...], fields: str
) -> list[Row]:
    if not keys or not exists(database, table):
        return []
    statement = (
        "SELECT " + fields + " FROM " + table + " WHERE " + column  # noqa: S608
        + " IN (" + _marks(len(keys)) + ") ORDER BY 1 LIMIT ?"
    )  # Identifiers are module constants; every value is bound.
    rows = text_cursor(database, statement, (*keys, MAX_ROWS + 1)).fetchall()
    if len(rows) > MAX_ROWS:
        code = "purge_inventory_budget"
        raise PurgeError(code)
    return [tuple(row) for row in rows]


def _intent_identity(scope: PurgeScope, row: Row) -> str:
    try:
        intent = _Intent.model_validate_json(row[3])
    except ValidationError as error:
        code = "purge_intent_unreadable"
        raise PurgeError(code) from error
    reference = HostRef(
        project=scope.project,
        goal_id=row[1],
        operation_id=row[0],
        epoch=intent.authority_epoch,
        owner_id=intent.owner_id,
    )
    return sha256(reference.model_dump_json().encode()).hexdigest()


def _clock_blockers(database: Connection, scope: PurgeScope) -> list[str]:
    blockers: list[str] = []
    for row in _rows(database, "continuation_clocks", "goal_id", scope.goal_ids, "goal_id,data"):
        try:
            clock = _Clock.model_validate_json(row[1])
        except ValidationError as error:
            code = "purge_clock_unreadable"
            raise PurgeError(code) from error
        if clock.active_intent_id or clock.hold.get("kind") == "unknown":
            blockers.append("目标仍有续跑轮次所有者或待核对状态，" + row[0])
        if any(item.status == "pending" for item in clock.obligations):
            blockers.append("目标仍有未完成的收尾义务，" + row[0])
    return blockers


def _unique(rows: list[Row]) -> list[Row]:
    return sorted(set(rows))


def inventory(database: Connection, scope: PurgeScope) -> PurgeInventory:
    """Read only: list every identity and the live state that must stay protected."""
    intents = _rows(
        database, "continuation_intents", "goal_id", scope.goal_ids, "id,goal_id,status,data"
    )
    identities = [_intent_identity(scope, row) for row in intents]
    owners = tuple("goal_" + identity for identity in identities)
    intent_ids = tuple(row[0] for row in intents)
    requests = _unique(
        _rows(database, "brain_requests", "owner", owners, "owner,request_id,state")
        + _rows(database, "brain_requests", "task_id", scope.task_ids, "owner,request_id,state")
    )
    steps = _rows(database, "goal_graph_steps", "intent_id", intent_ids, "intent_id,step,status")
    queue = _unique(
        _rows(database, "goal_task_queue", "goal_id", scope.goal_ids, "task_id,phase")
        + _rows(database, "goal_task_queue", "task_id", scope.task_ids, "task_id,phase")
    )
    starts = _unique(
        _rows(database, "goal_task_initializations", "goal_id", scope.goal_ids, "task_id,status")
        + _rows(database, "goal_task_initializations", "task_id", scope.task_ids, "task_id,status")
    )
    branches = _rows(database, "goal_task_branches", "goal_id", scope.goal_ids, "branch_id,status")
    uses = _rows(
        database, "human_decision_uses", "task_id", scope.task_ids, "decision_id,stage_id"
    )
    others = {
        table: _rows(database, table, "goal_id", scope.goal_ids, "goal_id")
        for table in (
            "goal_events",
            "human_decisions",
            "plan_revisions",
            "goal_contracts",
            "goal_requirements",
        )
    }
    blockers = [
        *("续跑轮次仍在进行或待核对，" + row[0] for row in intents if row[2] in ACTIVE_INTENTS),
        *_clock_blockers(database, scope),
        *("调用结果仍待核对，" + row[1] for row in requests if row[2] in OPEN_REQUESTS),
        *("主脑阶段仍在进行，" + row[0] for row in steps if row[2] == "inflight"),
        *("执行队列仍未结束，" + row[0] for row in queue if row[1] in OPEN_QUEUE),
        *("任务初始化尚未完成，" + row[0] for row in starts if row[1] != "ready"),
        *("阶段分支正在派发，" + row[0] for row in branches if row[1] == "open"),
    ]
    counts = (
        ("continuation_intents", len(intents)),
        ("brain_requests", len(requests)),
        ("goal_graph_steps", len(steps)),
        ("goal_task_queue", len(queue)),
        ("goal_task_initializations", len(starts)),
        ("goal_task_branches", len(branches)),
        ("human_decision_uses", len(uses)),
        *((table, len(rows)) for table, rows in others.items()),
    )
    digest = sha256(repr(counts).encode())
    for row in sorted(
        [
            *((row[0], row[1], row[2]) for row in intents),
            *requests,
            *steps,
            *queue,
            *starts,
            *branches,
            *uses,
        ]
    ):
        digest.update(repr(row).encode())
    return PurgeInventory(
        intent_ids=intent_ids,
        effect_owners=owners,
        goal_threads=tuple("native-goal-v1:" + identity for identity in identities),
        task_threads=tuple("native-task-v1:" + row[0] for row in branches),
        counts=counts,
        blockers=tuple(dict.fromkeys(blockers)),
        digest=digest.hexdigest(),
    )


def _delete(database: Connection, table: str, column: str, keys: tuple[str, ...]) -> None:
    if keys and exists(database, table):
        statement = (
            "DELETE FROM " + table + " WHERE " + column  # noqa: S608
            + " IN (" + _marks(len(keys)) + ")"
        )  # Identifiers are module constants; every value is bound.
        _ = text_cursor(database, statement, keys)


def purge(database: Connection, scope: PurgeScope, expected: PurgeInventory) -> PurgeInventory:
    """Delete inside the caller's transaction after the inventory is unchanged and unblocked."""
    if not database.in_transaction:
        code = "purge_transaction_required"
        raise PurgeError(code)
    current = inventory(database, scope)
    if current != expected:
        code = "purge_inventory_changed"
        raise PurgeError(code)
    if current.blockers:
        code = "purge_protected_records"
        raise PurgeError(code)
    _delete(database, "goal_graph_steps", "intent_id", current.intent_ids)
    _delete(database, "brain_requests", "owner", current.effect_owners)
    for table in TASK_TABLES:
        _delete(database, table, "task_id", scope.task_ids)
    for table in GOAL_TABLES:
        _delete(database, table, "goal_id", scope.goal_ids)
    return current
