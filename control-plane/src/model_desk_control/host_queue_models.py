"""A task queue receipt is distinct from model execution and independent acceptance."""

import json
from enum import StrEnum
from hashlib import sha256
from typing import Final, Literal

from pydantic import Field, JsonValue, TypeAdapter

from model_desk_control.host_flow import HostRecord
from model_desk_control.host_trace_models import CaptureId, RecordId, TaskObservation

MAX_RECORD: Final = 256 * 1024
INPUT_FIELDS: Final = (
    "id",
    "project",
    "goal_id",
    "stage_id",
    "engine",
    "kind",
    "model_id",
    "model",
    "files",
    "readonly_files",
    "files_readonly",
    "timeout",
    "test_command",
    "request_output_tokens",
    "cli_runtime",
    "engineering_policy",
    "authority_epoch",
    "goal_contract_revision",
    "goal_contract",
    "automatic_goal",
    "workspace_binding_id",
    "goal_dispatch_request_id",
    "goal_round_id",
    "goal_action_payload_digest",
    "delegation",
    "route",
    "brain",
    "owner",
    "request_id",
    "parent_task_id",
    "rework_of",
    "execution_access_mode",
    "workbuddy_connection",
    "workbuddy_tool_mode",
    "workbuddy_tool_revision",
    "workbuddy_access_mode",
    "pi_session_id",
    "pi_session_file",
    "dsh_session_key",
    "dsh_session_id",
    "relay_port",
    "connection_fingerprint",
    "native_session_id",
    "cli_launch_protocol",
    "queue_protocol",
)
JSON_RECORD: Final = TypeAdapter(dict[str, JsonValue])


class QueueError(ValueError):
    """A queue cannot prove that this delivery is still authorized or unstarted."""

    def __init__(self, code: str) -> None:
        """Keep diagnostics stable and independent of task content."""
        self.code: str = code
        super().__init__(code)


class Phase(StrEnum):
    """Only prelaunch phases can be reassigned after exact owner exit."""

    READY = "ready"
    QUEUED = "queued"
    CLAIMED = "claimed"
    ARMED = "armed"
    STARTED = "started"
    DONE = "done"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


class Process(HostRecord):
    """A PID without a birth receipt is never proof of safe process ownership."""

    pid: int = Field(gt=0)
    birth: float | None = None


class Owner(HostRecord):
    """One runtime incarnation, not merely a reusable process ID."""

    instance: RecordId
    process: Process


class Ticket(HostRecord):
    """Immutable original task identity and actual launch input digest."""

    version: Literal[1] = 1
    project: str
    goal_id: RecordId
    stage_id: RecordId
    task_id: RecordId
    epoch: int = Field(gt=0)
    round_id: RecordId
    request_id: RecordId
    payload_digest: str
    input_digest: str


class Delivery(HostRecord):
    """One bounded callback token and the runtime that owns it."""

    identity: RecordId
    owner: Owner


class QueueState(HostRecord):
    """Mutable delivery position, with every transition retained in the original audit log."""

    ticket: Ticket
    phase: Phase = Phase.READY
    delivery_id: CaptureId = ""
    owner: Owner | None = None
    acknowledged: bool = False
    launcher: Process | None = None
    reason: CaptureId = ""


def ticket(task_json: str, prompt: str) -> Ticket:
    """Bind a complete saved input, not arbitrary fields passed by a queued callback."""
    if max(len(task_json.encode()), len(prompt.encode())) > MAX_RECORD:
        code = "queue_input_budget"
        raise QueueError(code)
    task = TaskObservation.model_validate_json(task_json)
    if not (
        task.goal_id
        and task.stage_id
        and task.authority_epoch
        and task.goal_round_id
        and task.goal_dispatch_request_id
        and task.goal_action_payload_digest
    ):
        code = "queue_original_identity_missing"
        raise QueueError(code)
    source = JSON_RECORD.validate_json(task_json)
    content = json.dumps({key: source.get(key) for key in INPUT_FIELDS}, sort_keys=True)
    return Ticket(
        project=task.project,
        goal_id=task.goal_id,
        stage_id=task.stage_id,
        task_id=task.id,
        epoch=task.authority_epoch,
        round_id=task.goal_round_id,
        request_id=task.goal_dispatch_request_id,
        payload_digest=task.goal_action_payload_digest,
        input_digest=sha256((content + "\n" + prompt).encode()).hexdigest(),
    )
