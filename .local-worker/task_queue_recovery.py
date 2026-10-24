"""Project-scoped recovery decisions without a new worker or timer."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Final

import continuation_store
import control_bootstrap
import model_registry as registry
import psutil
import stage_store
import task_queue
from pydantic import TypeAdapter

if TYPE_CHECKING:
    from sqlite3 import Connection

    from task_queue import Record

    from model_desk_control.host_queue_models import QueueState, Ticket

LOGGER: Final = logging.getLogger(__name__)
BIRTH_TOLERANCE: Final = 0.01


def retained(database: Connection, state: Record) -> bool:
    """Keep a proven unstarted task or an exactly identified live launcher at startup."""
    if task_queue.prelaunch(database, state):
        return True
    if not task_queue.enabled(state):
        return False
    control_bootstrap.bind_source()
    from model_desk_control.host_queue_models import Phase
    from model_desk_control.host_queue_store import read

    saved = read(database, str(state["id"]))
    if (
        saved is None
        or saved.phase != Phase.STARTED
        or saved.ticket.project != state.get("project")
        or saved.ticket.goal_id != state.get("goal_id")
        or saved.ticket.stage_id != state.get("stage_id")
        or state.get("status") not in {"queued", "running"}
        or saved.launcher is None
        or saved.launcher.birth is None
    ):
        return False
    try:
        process = psutil.Process(saved.launcher.pid)
        return (
            abs(process.create_time() - saved.launcher.birth) <= BIRTH_TOLERANCE
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.Error:
        return False


def cancelled(state: Record) -> None:
    """Retire only deliveries for which a durable cancellation flag already exists."""
    if not task_queue.enabled(state):
        return
    control_bootstrap.bind_source()
    from model_desk_control.host_queue import retire
    from model_desk_control.host_queue_models import Phase
    from model_desk_control.native_output import owned_path

    saved = task_queue.observed(state)
    if saved is None:
        return
    ticket = saved.ticket
    flag = owned_path(
        Path(ticket.project), Path(".model-desk/runs") / ticket.task_id / "cancelled.flag"
    )
    if not flag.is_file():
        return
    with continuation_store.mutation(ticket.project, ticket.goal_id) as database:
        _ = retire(database, saved, Phase.CANCELLED, "task_cancelled")


def _block_inputs(saved: QueueState, code: str) -> bool:
    """Atomically retire the same original stage's delivery before reporting invalid inputs."""
    control_bootstrap.bind_source()
    from model_desk_control.host_queue import retire
    from model_desk_control.host_queue_models import Phase
    from model_desk_control.host_queue_store import origin
    from model_desk_control.native_output import NativeReadError, owned_path, read_metadata

    ticket = saved.ticket
    with continuation_store.mutation(ticket.project, ticket.goal_id) as database:
        origin(database, ticket)
        stopped = retire(database, saved, Phase.BLOCKED, code)
        if stopped is None:
            return False
        try:
            file = owned_path(
                Path(ticket.project), Path(".model-desk/runs") / ticket.task_id / "task.json"
            )
            state = TypeAdapter(task_queue.Record).validate_json(read_metadata(file))
            if (
                state.get("id"),
                state.get("project"),
                state.get("goal_id"),
                state.get("stage_id"),
            ) == (ticket.task_id, ticket.project, ticket.goal_id, ticket.stage_id) and state.get(
                "status"
            ) == "queued":
                _ = registry.patch_task(
                    file.parent,
                    status="recovery_pending",
                    phase="待恢复 (队列输入需要核对)",
                    recovery={"kind": "queue_blocked", "reason": code, "auto_resend": False},
                )
        except (OSError, ValueError, NativeReadError):
            LOGGER.warning("Original task metadata cannot be updated for %s", ticket.task_id)
    return True


def blocked(saved: QueueState, code: str) -> None:
    """Keep the original goal lock until the same stage's recovery reason is recorded."""
    ticket = saved.ticket
    with continuation_store.locked(ticket.project, ticket.goal_id):
        if not _block_inputs(saved, code):
            return
        try:
            _ = stage_store.mark_recovery_pending(
                ticket.stage_id,
                "队列输入需要核对: " + code,
                actor="queue-driver",
                project=ticket.project,
            )
        except (OSError, ValueError):
            LOGGER.warning("Original stage recovery remains unresolved for %s", ticket.task_id)


def superseded(state: Record, reason: str) -> None:
    """Retire an unstarted old-revision delivery while preserving its task history."""
    if not task_queue.enabled(state):
        return
    control_bootstrap.bind_source()
    from model_desk_control.host_queue import retire
    from model_desk_control.host_queue_models import Phase
    from model_desk_control.native_output import NativeReadError, owned_path, read_metadata

    saved = task_queue.observed(state)
    if saved is None:
        return
    ticket = saved.ticket
    with continuation_store.locked(ticket.project, ticket.goal_id):
        with continuation_store.mutation(ticket.project, ticket.goal_id) as database:
            retired = retire(database, saved, Phase.CANCELLED, "requirements_superseded")
        if retired is None:
            return
        try:
            file = owned_path(
                Path(ticket.project), Path(".model-desk/runs") / ticket.task_id / "task.json"
            )
            current = TypeAdapter(task_queue.Record).validate_json(read_metadata(file))
            if (
                current.get("id"), current.get("project"), current.get("goal_id"),
                current.get("stage_id"), current.get("status"),
            ) == (
                ticket.task_id, ticket.project, ticket.goal_id, ticket.stage_id, "queued",
            ):
                updated = registry.patch_task(
                    file.parent,
                    status="failed",
                    phase="运行中需求已更新，旧排队任务未启动",
                    ended_at=registry.now(),
                    error=reason,
                    recovery={
                        "kind": "requirements_superseded",
                        "reason": reason,
                        "auto_resend": False,
                    },
                )
                import goal_events

                goal_events.sync_task(updated)
                _ = stage_store.mark_requirements_superseded(
                    ticket.stage_id,
                    ticket.task_id,
                    reason,
                    actor="queue-driver",
                    project=ticket.project,
                )
        except (OSError, ValueError, NativeReadError):
            LOGGER.warning("Superseded task metadata cannot be updated for %s", ticket.task_id)


def refused(ticket: Ticket, delivery_id: str, code: str) -> None:
    """An invalid callback is reported only against its own still-current queue token."""
    control_bootstrap.bind_source()
    from model_desk_control.host_queue_store import read

    with continuation_store.readonly(ticket.project) as database:
        saved = read(database, ticket.task_id) if database else None
    if saved is not None and saved.ticket == ticket and saved.delivery_id == delivery_id:
        blocked(saved, code)
