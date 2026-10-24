"""Project-owned task and stage facts used only when a human opens reconciliation."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import Final, Literal

from pydantic import TypeAdapter, ValidationError

from model_desk_control.database import text_cursor
from model_desk_control.host_resolution_models import TaskFact
from model_desk_control.host_resolution_records import digest
from model_desk_control.host_trace_models import RecordId, TaskObservation
from model_desk_control.native_output import (
    METADATA_BYTES,
    NativeReadError,
    owned_path,
    read_metadata,
)

type ProcessProbe = Callable[[int, float], Literal["running", "exited", "unknown"]]
MAX_STAGES: Final = 500
MAX_TASKS: Final = 500
MAX_STAGE_BYTES: Final = 2 * 1024 * 1024
MAX_TASK_BYTES: Final = 2 * 1024 * 1024
TASK_IDS: Final = TypeAdapter(tuple[RecordId, ...])
FILES: Final = TypeAdapter(tuple[str, ...])
TERMINAL: Final = frozenset({"completed", "failed", "cancelled"})


class ResolutionTask(TaskObservation):
    """Unlaunched cancellation must retain the original automatic authority fence."""

    automatic_goal: bool = False
    cli_launch_protocol: Literal[2] | None = None
    native_launch_state: Literal["", "armed", "started", "not_started"] = ""
    title: str = ""


@dataclass(frozen=True, slots=True)
class StageFacts:
    """The window is complete or its blocker explicitly prevents disposition."""

    task_ids: tuple[str, ...]
    files: tuple[str, ...]
    digest: str
    blockers: tuple[str, ...]


def stages(database: Connection, project: Path, goal_id: str) -> StageFacts:
    """Inspect every goal association within a fixed byte and row budget."""
    rows = text_cursor(
        database,
        """SELECT id,project,status,active_task_id,
        CAST(length(CAST(task_ids AS BLOB))+length(CAST(files AS BLOB)) AS TEXT)
        FROM stages WHERE goal_id=? ORDER BY id LIMIT ?""",
        (goal_id, MAX_STAGES + 1),
    ).fetchall()
    blockers: list[str] = []
    if len(rows) > MAX_STAGES:
        blockers.append("阶段历史超过核对窗口，不能在不完整清单上结束轮次")
    tasks: list[str] = []
    files: list[str] = []
    signatures: list[list[str]] = []
    remaining = MAX_STAGE_BYTES
    for row in rows[:MAX_STAGES]:
        if row[1] != str(project):
            blockers.append("阶段记录的项目归属不一致")
        if int(row[4]) > remaining:
            blockers.append("阶段任务清单超过核对读取预算")
            continue
        saved = text_cursor(
            database,
            """SELECT task_ids,files FROM stages WHERE id=? AND goal_id=?
            AND length(CAST(task_ids AS BLOB))+length(CAST(files AS BLOB))<=?""",
            (row[0], goal_id, remaining),
        ).fetchone()
        if saved is None:
            blockers.append("阶段清单在核对期间发生变化")
            continue
        remaining -= len(saved[0].encode()) + len(saved[1].encode())
        signatures.append([*row[:4], digest(saved[0]), digest(saved[1])])
        try:
            tasks.extend(TASK_IDS.validate_json(saved[0]))
            files.extend(FILES.validate_json(saved[1]))
            if row[3]:
                tasks.extend(TASK_IDS.validate_python((row[3],)))
        except ValidationError:
            blockers.append("阶段的任务或文件清单无法核对")
    return StageFacts(
        tuple(dict.fromkeys(tasks)),
        tuple(dict.fromkeys(files)),
        digest(json.dumps(signatures)),
        tuple(dict.fromkeys(blockers)),
    )


def _process(
    task: ResolutionTask, project: Path, epoch: int, probe: ProcessProbe
) -> Literal["running", "exited", "unknown", "not_started", "not_applicable"]:
    if task.process_pid is not None and task.process_birth is not None:
        if task.process_pid > 0 and task.process_birth > 0:
            return probe(task.process_pid, task.process_birth)
        return "unknown"
    if task.process_pid is not None or task.process_birth is not None:
        return "unknown"
    if task.engine == "api" and task.status in TERMINAL:
        return "not_applicable"
    if (
        task.status in TERMINAL
        and task.automatic_goal
        and task.cli_launch_protocol is not None
        and task.native_launch_state in {"", "not_started"}
        and task.authority_epoch is not None
        and task.authority_epoch <= epoch
        and task.native_output_capture == 0
        and (
            task.status != "cancelled"
            or owned_path(project, Path(".model-desk/runs") / task.id / "cancelled.flag").is_file()
        )
    ):
        return "not_started"
    return "unknown"


def tasks(
    project: Path, goal_id: str, task_ids: tuple[str, ...], epoch: int, probe: ProcessProbe
) -> tuple[TaskFact, ...]:
    """Do not fall back to a registry path or trust a terminal native label alone."""
    if len(task_ids) > MAX_TASKS:
        return (
            TaskFact(
                task_id="window_limit",
                status="unknown",
                engine="",
                process_state="unknown",
                metadata_digest="",
                blocker="任务关联超过完整核对窗口",
            ),
        )
    observed: list[TaskFact] = []
    remaining = MAX_TASK_BYTES
    for task_id in task_ids:
        validated = TASK_IDS.validate_python((task_id,))[0]
        try:
            path = owned_path(project, Path(".model-desk/runs") / validated / "task.json")
            raw = read_metadata(path, byte_limit=min(remaining, METADATA_BYTES))
            remaining -= len(raw.encode())
            task = ResolutionTask.model_validate_json(raw)
            if task.id != validated or task.project != str(project) or task.goal_id != goal_id:
                observed.append(
                    TaskFact(
                        task_id=validated,
                        status="unknown",
                        engine="",
                        process_state="unknown",
                        metadata_digest=digest(raw),
                        blocker="任务归属不符",
                    )
                )
                continue
            state = _process(task, project, epoch, probe)
            blocker = (
                "请先停止此任务并核对执行进程"
                if task.status not in TERMINAL
                else "执行进程尚未确认退出"
                if state not in {"exited", "not_started", "not_applicable"}
                else ""
            )
            observed.append(
                TaskFact(
                    task_id=validated,
                    status=task.status,
                    engine=task.engine,
                    process_state=state,
                    metadata_digest=digest(raw),
                    blocker=blocker,
                    title=task.title,
                )
            )
        except (OSError, NativeReadError, ValidationError, UnicodeError):
            observed.append(
                TaskFact(
                    task_id=validated,
                    status="unknown",
                    engine="",
                    process_state="unknown",
                    metadata_digest="",
                    blocker="原任务记录无法完整核对",
                )
            )
    return tuple(observed)
