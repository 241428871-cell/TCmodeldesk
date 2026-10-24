"""Durable, versioned requirements inserted while a goal is running."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final, Literal, NotRequired, TypedDict, cast
from uuid import uuid4

import continuation_store as store
import project_storage
import stage_store
from pydantic import TypeAdapter

from model_desk_control.database import text_cursor

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

RequirementKind = Literal["requirement", "direction_change"]
MAX_TEXT: Final = 20_000
MAX_ROWS: Final = 200
MAX_REQUEST_ID: Final = 100
MISSING_GOAL: Final = "目标不存在或不属于当前项目"
INVALID_CONTENT: Final = "最新需求不能为空，且最多 20000 个字符"
INVALID_KIND: Final = "需求类型必须是增量需求或方向调整"
INVALID_REQUEST: Final = "请求标识格式无效"
REPLAY_MISMATCH: Final = "相同请求标识不能改写原需求"
UNAVAILABLE_GOAL: Final = "只能向尚未结束且已配置的目标插入需求"
STOPPED_GOAL: Final = "目标已停止；请明确恢复目标后再加入新需求"


class Requirement(TypedDict):
    """One immutable human requirement plus its processing receipt."""

    id: str
    goal_id: str
    version: int
    kind: RequirementKind
    text: str
    status: str
    created_at: str
    applied_at: str
    applied_intent_id: str
    applies_after: str
    interrupted_task_ids: NotRequired[list[str]]
    interrupt_pending_task_ids: NotRequired[list[str]]
    interrupt_state: NotRequired[str]


class RequirementError(ValueError):
    """A requirement insertion is invalid or targets an unavailable goal."""


_ADAPTER: Final = TypeAdapter(Requirement)


def _schema(database: sqlite3.Connection) -> None:
    _ = database.execute(
        """CREATE TABLE IF NOT EXISTS goal_requirements (
        id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, version INTEGER NOT NULL,
        kind TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
        UNIQUE(goal_id, version))"""
    )


def _exists(database: sqlite3.Connection) -> bool:
    query = "SELECT CAST(1 AS TEXT) FROM sqlite_master WHERE name='goal_requirements'"
    return text_cursor(
        database,
        query,
    ).fetchone() is not None


def _parse(row: Sequence[str]) -> Requirement:
    return _ADAPTER.validate_json(row[0])


def latest_version(database: sqlite3.Connection, goal_id: str) -> int:
    """Return the newest durable version without creating storage on a read."""
    if not _exists(database):
        return 0
    query = "SELECT CAST(COALESCE(MAX(version),0) AS TEXT) FROM goal_requirements WHERE goal_id=?"
    row = text_cursor(database, query, (goal_id,)).fetchone()
    return int(row[0]) if row is not None else 0


def list_for_goal(project: str, goal_id: str, *, through: int | None = None) -> list[Requirement]:
    """Read bounded requirement history; this never starts work or creates tables."""
    project = project_storage.normalize(project)
    with store.readonly(project) as database:
        if database is None or not _exists(database):
            return []
        if stage_store.get_goal(goal_id, project) is None:
            _missing_goal()
        params: tuple[str] | tuple[str, int]
        query = "SELECT data FROM goal_requirements WHERE goal_id=?"
        if through is None:
            params = (goal_id,)
        else:
            query += " AND version<=?"
            params = (goal_id, through)
        rows = text_cursor(
            database, query + " ORDER BY version DESC LIMIT ?", (*params, MAX_ROWS)
        ).fetchall()
    return [_parse(row) for row in reversed(rows)]


def _missing_goal() -> None:
    raise RequirementError(MISSING_GOAL)


def _interrupt_direction_change(
    project: str, goal_id: str, version: int
) -> tuple[list[str], list[str]]:
    """Fence older active tasks after the requirement itself is durable.

    The cancellation flag is the shared executor boundary: native workers see
    it before writeback and running adapters stop their exact child.  The stage
    becomes immutable superseded history immediately, while its durable lease
    remains held until the original worker proves exit.  A replacement may be
    planned now, but cannot acquire conflicting resources early.
    """
    import model_registry as registry
    import task_queue_recovery

    interrupted: list[str] = []
    pending: list[str] = []
    reason = f"方向调整 R{version} 已持久化；旧任务停止且不得写回，下一安全轮次重新规划"
    for stage in stage_store.list_stages(goal_id, project):
        task_id = str(stage.get("active_task_id") or "")
        if not task_id or stage.get("status") not in {"dispatched", "running"}:
            continue
        try:
            run = registry.task_dir(task_id, project)
            state = registry.read_task(run)
            claimed = state.get("goal_requirements_revision")
            marker = state.get("requirements_interrupt")
            retry_marker = isinstance(marker, dict) and marker.get("version") == version
            current_task = (state.get("status") in {"queued", "running"}
                            and isinstance(claimed, int) and not isinstance(claimed, bool)
                            and claimed < version)
            if (state.get("goal_id") != goal_id or state.get("project") != project
                    or not (current_task or retry_marker)):
                continue
            (run / "cancelled.flag").touch(exist_ok=True)
            task_queue_recovery.cancelled(state)
            _ = registry.patch_task(
                run,
                cancel_record={
                    "at": registry.now(),
                    "actor": "requirement-stream",
                    "reason": reason,
                },
                requirements_interrupt={"version": version, "kind": "direction_change"},
            )
            _ = stage_store.mark_requirements_superseded(
                str(stage["id"]), task_id, reason,
                actor="requirement-stream", project=project,
            )
            interrupted.append(task_id)
        except (OSError, ValueError, RuntimeError):
            # The requirement remains pending and visible.  Never claim an
            # interruption unless both its durable flag and stage marker exist.
            pending.append(task_id)
    return interrupted, pending


def _record_interruptions(
    project: str,
    requirement: Requirement,
    tasks: list[str],
    pending: list[str],
) -> Requirement:
    """Attach a replayable interruption receipt to the immutable requirement."""
    with store.mutation(project, requirement["goal_id"]) as database:
        row = text_cursor(
            database, "SELECT data FROM goal_requirements WHERE id=?", (requirement["id"],)
        ).fetchone()
        if row is None:
            return requirement
        current = _parse(row)
        interrupted = list(dict.fromkeys([*current.get("interrupted_task_ids", []), *tasks]))
        unresolved = list(dict.fromkeys([*current.get("interrupt_pending_task_ids", []), *pending]))
        unresolved = [task_id for task_id in unresolved if task_id not in interrupted]
        current["interrupted_task_ids"] = interrupted
        current["interrupt_pending_task_ids"] = unresolved
        current["interrupt_state"] = (
            "incomplete" if unresolved else "requested" if interrupted else "not_needed"
        )
        _ = database.execute(
            "UPDATE goal_requirements SET data=? WHERE id=?",
            (json.dumps(current, ensure_ascii=False), current["id"]),
        )
        return current


def insert(
    project: str,
    goal_id: str,
    text: str,
    *,
    kind: str = "requirement",
    request_id: str = "",
) -> Requirement:
    """Persist one additive requirement and make the next safe round immediately due."""
    content = text.strip()
    if not content or len(content) > MAX_TEXT:
        raise RequirementError(INVALID_CONTENT)
    if kind not in ("requirement", "direction_change"):
        raise RequirementError(INVALID_KIND)
    requirement_kind = cast(RequirementKind, kind)
    if request_id and (
        len(request_id) > MAX_REQUEST_ID or not request_id.replace("-", "").isalnum()
    ):
        raise RequirementError(INVALID_REQUEST)
    identity = "requirement_" + (request_id or uuid4().hex)
    project = project_storage.normalize(project)
    replayed = False
    with store.mutation(project, goal_id) as database:
        _schema(database)
        replay = text_cursor(
            database, "SELECT data FROM goal_requirements WHERE id=?", (identity,)
        ).fetchone()
        if replay is not None:
            previous = _parse(replay)
            if (
                previous["goal_id"] != goal_id
                or previous["text"] != content
                or previous["kind"] != requirement_kind
            ):
                raise RequirementError(REPLAY_MISMATCH)
            record = previous
            replayed = True
        else:
            goal = text_cursor(
                database, "SELECT status FROM goals WHERE id=?", (goal_id,)
            ).fetchone()
            clock = store.read(database, goal_id)
            if (
                goal is None
                or goal[0] != stage_store.GOAL_ACTIVE
                or clock is None
                or clock["closed"]
            ):
                raise RequirementError(UNAVAILABLE_GOAL)
            if clock["user_intent"] == "STOP":
                raise RequirementError(STOPPED_GOAL)
            version = latest_version(database, goal_id) + 1
            applies_after = (
                "current_round" if store.active(database, clock) is not None else "next_round"
            )
            record = Requirement(
                id=identity,
                goal_id=goal_id,
                version=version,
                kind=requirement_kind,
                text=content,
                status="pending",
                created_at=store.iso(),
                applied_at="",
                applied_intent_id="",
                applies_after=applies_after,
            )
            insert_query = "INSERT INTO goal_requirements VALUES (?,?,?,?,?,?)"
            _ = database.execute(
                insert_query,
                (identity, goal_id, version, requirement_kind, "pending",
                 json.dumps(record, ensure_ascii=False)),
            )
            clock["requirements_revision"] = version
            if clock["enabled"] and clock["user_intent"] == "RUN":
                clock["next_due_at"] = store.iso()
                _ = clock.pop("event_not_before", None)
            store.write(database, clock)
    if requirement_kind == "direction_change":
        tasks, pending = _interrupt_direction_change(project, goal_id, int(record["version"]))
        if not replayed or "interrupt_state" not in record or record["interrupt_state"] == "incomplete":
            record = _record_interruptions(project, record, tasks, pending)
    return record


def mark_applied(
    database: sqlite3.Connection, goal_id: str, through: int, intent_id: str
) -> None:
    """Mark only the versions captured by this successful brain round."""
    if through <= 0 or not _exists(database):
        return
    query = (
        "SELECT data FROM goal_requirements WHERE goal_id=? AND version<=? "
        "AND status='pending' ORDER BY version"
    )
    rows = text_cursor(
        database,
        query,
        (goal_id, through),
    ).fetchall()
    for row in rows:
        record = _parse(row)
        record.update(status="applied", applied_at=store.iso(), applied_intent_id=intent_id)
        _ = database.execute(
            "UPDATE goal_requirements SET status='applied',data=? WHERE id=?",
            (json.dumps(record, ensure_ascii=False), record["id"]),
        )
