"""Deliver original queued tasks through the existing executor and continuation scanner."""

from __future__ import annotations

import logging
import os
from json import JSONDecodeError
from typing import TYPE_CHECKING, Final, Protocol
from uuid import uuid4

import continuation_clock
import continuation_store
import control_bootstrap
import governance_context
import platform_runtime
import psutil
import task_queue
import task_queue_recovery

if TYPE_CHECKING:
    from concurrent.futures import Executor

    from task_queue import Record

    from model_desk_control.host_queue_models import Owner

INSTANCE: Final = "queue_owner_" + uuid4().hex
PROCESS_BIRTH_TOLERANCE: Final = 0.01
LOGGER: Final = logging.getLogger(__name__)


class Consumer(Protocol):
    """A delivery token cannot be inferred from task text or the newest queue entry."""

    def __call__(self, task_id: str, *, delivery_id: str = "") -> None:
        """Execute only this specific queued delivery."""
        ...


def owner() -> Owner:
    """Read the current runtime identity only on an explicit queue operation."""
    control_bootstrap.bind_source()
    from model_desk_control.host_queue_models import Owner, Process, QueueError

    birth = platform_runtime.process_birth(os.getpid())
    if birth is None:
        code = "queue_runtime_identity_unavailable"
        raise QueueError(code)
    return Owner(instance=INSTANCE, process=Process(pid=os.getpid(), birth=birth))


def exited(previous: Owner) -> bool:
    """Unreadable process state is not evidence that an old queue owner exited."""
    if previous.process.birth is None or previous.instance == INSTANCE:
        return False
    try:
        process = psutil.Process(previous.process.pid)
        return (
            abs(process.create_time() - previous.process.birth) > PROCESS_BIRTH_TOLERANCE
            or process.status() == psutil.STATUS_ZOMBIE
        )
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return True
    except psutil.Error:
        return False


def offer(state: Record, executor: Executor, consume: Consumer, *, resume: bool = False) -> bool:
    """A submission exception revokes its token while the goal lock still fences callbacks."""
    control_bootstrap.bind_source()
    from pydantic import ValidationError

    from model_desk_control.host_queue import acknowledge, advance, reserve
    from model_desk_control.host_queue_models import Delivery, Phase, QueueError
    from model_desk_control.native_output import NativeReadError

    saved = task_queue.observed(state)
    if saved is None:
        return False
    former = saved.owner if saved.owner is not None and exited(saved.owner) else None
    if (
        saved.phase != Phase.READY
        and former is None
        and not (resume and saved.phase == Phase.BLOCKED)
    ):
        return False
    source = task_queue.load_input(saved.ticket.project, saved.ticket.task_id)
    if source.state.get("status") != "queued":
        task_queue_recovery.cancelled(source.state)
        return False
    delivery = "delivery_" + uuid4().hex
    with governance_context.launch_authority(source.state):
        with continuation_store.mutation(source.ticket.project, source.ticket.goal_id) as database:
            selected = reserve(
                database,
                source.ticket,
                Delivery(owner=owner(), identity=delivery),
                exited_owner=former,
                resume=resume,
            )
        if selected is None:
            return False

        def execute(task_id: str) -> None:
            try:
                task_queue.verify_input(source.ticket)
                consume(task_id, delivery_id=delivery)
            except (
                QueueError,
                NativeReadError,
                ValidationError,
                JSONDecodeError,
                UnicodeError,
                OSError,
            ):
                task_queue_recovery.refused(source.ticket, delivery, "queue_callback_input_invalid")

        try:
            _ = executor.submit(execute, source.ticket.task_id)
        except (RuntimeError, OSError):
            with continuation_store.mutation(
                source.ticket.project, source.ticket.goal_id
            ) as database:
                _ = advance(database, source.ticket.task_id, delivery, Phase.READY)
            raise
        with continuation_store.mutation(source.ticket.project, source.ticket.goal_id) as database:
            acknowledge(database, source.ticket.task_id, delivery)
    return True


def recover(project: str, goal_id: str, executor: Executor, consume: Consumer) -> None:
    """The existing scanner reuses safe original tasks; it does not create another task or clock."""
    control_bootstrap.bind_source()
    from pydantic import ValidationError

    from model_desk_control.host_queue_models import QueueError
    from model_desk_control.host_queue_store import candidates, read
    from model_desk_control.native_output import NativeReadError

    guard = continuation_clock.dispatch_guard(project, goal_id)
    if not guard["allowed"]:
        return
    with continuation_store.readonly(project) as database:
        identities = candidates(database, goal_id) if database else ()
    for identity in identities:
        try:
            with continuation_store.readonly(project) as database:
                saved = read(database, identity) if database else None
            if saved is None:
                continue
            try:
                _ = offer({"project": project, "id": identity}, executor, consume)
            except continuation_store.RequirementsChangedError as error:
                source = task_queue.load_input(saved.ticket.project, saved.ticket.task_id)
                task_queue_recovery.superseded(source.state, str(error))
                continue
            except governance_context.DispatchBlocked:
                continue
            except (QueueError, NativeReadError, ValidationError, OSError) as error:
                code = (
                    error.code
                    if isinstance(error, (QueueError, NativeReadError))
                    else "queue_input_unreadable"
                )
                task_queue_recovery.blocked(saved, code)
        except (OSError, ValueError, RuntimeError):
            LOGGER.warning("Cannot reconcile original queue receipt for %s", identity)
