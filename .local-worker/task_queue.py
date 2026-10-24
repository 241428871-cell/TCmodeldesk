"""Original-project queue receipts at the actual worker-process boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import continuation_store
import control_bootstrap
import platform_runtime
from pydantic import JsonValue, TypeAdapter

if TYPE_CHECKING:
    from sqlite3 import Connection

    from model_desk_control.host_queue_models import QueueState, Ticket

type Record = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Input:
    """A bounded original task input, without registry fallback to another project."""

    ticket: Ticket
    state: Record


def load_input(project: str, task_id: str) -> Input:
    """Require fixed project-owned paths and a complete saved prompt."""
    control_bootstrap.bind_source()
    from model_desk_control.host_queue_models import QueueError, ticket
    from model_desk_control.host_trace_models import RecordId
    from model_desk_control.native_output import owned_path, read_metadata

    identity = TypeAdapter(RecordId).validate_python(task_id)
    root = Path(project).resolve()
    relative = Path(".model-desk/runs") / identity
    raw = read_metadata(owned_path(root, relative / "task.json"))
    prompt = read_metadata(owned_path(root, relative / "prompt.txt"))
    reference = ticket(raw, prompt)
    if reference.project != str(root) or reference.task_id != identity:
        code = "queue_project_identity"
        raise QueueError(code)
    return Input(reference, TypeAdapter(Record).validate_json(raw))


def enabled(state: Record) -> bool:
    """Only new explicitly marked managed tasks enter the durable delivery protocol."""
    version = state.get("queue_protocol")
    if version is None:
        return False
    if type(version) is not int or version != 1:
        control_bootstrap.bind_source()
        from model_desk_control.host_queue_models import QueueError

        code = "queue_protocol_unsupported"
        raise QueueError(code)
    return True


def verify_input(expected: Ticket) -> None:
    """Reject a changed or incomplete task before invoking the original queue consumer."""
    control_bootstrap.bind_source()
    from model_desk_control.host_queue_models import QueueError

    actual = load_input(expected.project, expected.task_id)
    if actual.ticket != expected:
        code = "queue_input_changed"
        raise QueueError(code)


def prepare(state: Record) -> None:
    """Called after original metadata initialization and before any executor submission."""
    control_bootstrap.bind_source()
    from model_desk_control.host_initialization_store import complete
    from model_desk_control.host_queue_store import prepare as save

    source = load_input(str(state["project"]), str(state["id"]))
    with continuation_store.mutation(source.ticket.project, source.ticket.goal_id) as database:
        _ = save(database, source.ticket)
        complete(database, source.ticket)


def observed(state: Record) -> QueueState | None:
    """Read an existing receipt without initialization or model activity."""
    control_bootstrap.bind_source()
    from model_desk_control.host_queue_store import read

    with continuation_store.readonly(str(state["project"])) as database:
        return read(database, str(state["id"])) if database is not None else None


def prelaunch(database: Connection, state: Record) -> bool:
    """Startup may retain an original stage when no launcher was armed."""
    if not enabled(state):
        return False
    control_bootstrap.bind_source()
    from model_desk_control.host_queue_models import Phase
    from model_desk_control.host_queue_store import read

    record = read(database, str(state["id"]))
    return (
        record is not None
        and record.phase in {Phase.READY, Phase.QUEUED, Phase.CLAIMED}
        and record.ticket.project == state.get("project")
        and record.ticket.goal_id == state.get("goal_id")
        and record.ticket.stage_id == state.get("stage_id")
        and state.get("status") == "queued"
    )


def begin(state: Record, delivery_id: str) -> bool:
    """Only the queued token can enter the worker; duplicate callbacks are inert."""
    if not enabled(state):
        if delivery_id:
            return False
        if state.get("project") and state.get("goal_id") and observed(state) is not None:
            control_bootstrap.bind_source()
            from model_desk_control.host_queue_models import QueueError

            code = "queue_protocol_missing"
            raise QueueError(code)
        return True
    control_bootstrap.bind_source()
    from model_desk_control.host_queue import advance
    from model_desk_control.host_queue_models import Phase
    from model_desk_control.host_queue_store import read

    project, goal_id, task_id = (str(state[key]) for key in ("project", "goal_id", "id"))
    with continuation_store.mutation(project, goal_id) as database:
        record = read(database, task_id)
        if record is None or record.delivery_id != delivery_id or record.phase != Phase.QUEUED:
            return False
        if record.ticket.project != project or record.ticket.goal_id != goal_id:
            from model_desk_control.host_queue_models import QueueError

            code = "queue_consumer_identity"
            raise QueueError(code)
        return advance(database, task_id, delivery_id, Phase.CLAIMED) is not None


def arm(state: Record, delivery_id: str) -> None:
    """The caller holds current goal authority through this marker and actual Popen."""
    if not enabled(state):
        return
    control_bootstrap.bind_source()
    from model_desk_control.host_queue import advance, require
    from model_desk_control.host_queue_models import Phase, QueueError

    source = load_input(str(state["project"]), str(state["id"]))
    with continuation_store.mutation(source.ticket.project, source.ticket.goal_id) as database:
        _ = require(database, source.ticket)
        if advance(database, source.ticket.task_id, delivery_id, Phase.ARMED) is None:
            code = "queue_launch_token_revoked"
            raise QueueError(code)


def started(state: Record, delivery_id: str, pid: int) -> None:
    """Persist the actual launcher identity before trusting worker completion."""
    if not enabled(state):
        return
    control_bootstrap.bind_source()
    from model_desk_control.host_queue import advance
    from model_desk_control.host_queue_models import Phase, Process, QueueError

    with continuation_store.mutation(str(state["project"]), str(state["goal_id"])) as database:
        process = Process(pid=pid, birth=platform_runtime.process_birth(pid))
        if (
            advance(database, str(state["id"]), delivery_id, Phase.STARTED, launcher=process)
            is None
        ):
            code = "queue_launcher_receipt_conflict"
            raise QueueError(code)


def outcome(
    state: Record, delivery_id: str, *, waited: bool = False, no_process: bool = False
) -> str:
    """A positive wait/no-start receipt differs from an ambiguous physical launch."""
    if not enabled(state):
        return "legacy"
    control_bootstrap.bind_source()
    from model_desk_control.host_queue import advance
    from model_desk_control.host_queue_models import Phase
    from model_desk_control.host_queue_store import read

    with continuation_store.mutation(str(state["project"]), str(state["goal_id"])) as database:
        previous = read(database, str(state["id"]))
        if previous is None or previous.delivery_id != delivery_id:
            return "stale"
        phase = (
            Phase.DONE
            if waited
            else (Phase.BLOCKED if no_process or previous.phase == Phase.CLAIMED else Phase.UNKNOWN)
        )
        result = advance(database, previous.ticket.task_id, delivery_id, phase)
        return (result or previous).phase.value
