"""Immutable native completion evidence for file-only recovery of the original task."""

import hashlib
import json
from pathlib import Path
from sqlite3 import Connection
from typing import Final, Literal

from pydantic import Field, JsonValue

from model_desk_control.file_writeback import WritebackError, WritebackPlan
from model_desk_control.host_flow import HostRecord
from model_desk_control.host_initialization_files import publish
from model_desk_control.host_queue_models import Phase, QueueState
from model_desk_control.host_queue_store import read, write
from model_desk_control.host_trace_models import CaptureId
from model_desk_control.native_output import owned_path, read_metadata

INPUTS: Final = (
    "project", "engine", "kind", "model_id", "model", "files", "goal_id", "stage_id",
    "authority_epoch", "goal_contract_revision", "test_command", "cli_runtime",
    "queue_protocol", "goal_round_id", "goal_dispatch_request_id", "goal_action_payload_digest",
    "automatic_goal", "goal_contract", "engineering_policy", "execution_access_mode",
)


class TestResult(HostRecord):
    """Evidence from the existing explicit test command, never an acceptance verdict."""

    exit_code: int
    output: str = Field(max_length=6000)


class WritebackReceipt(HostRecord):
    """One completed native result; it cannot authorize another model invocation."""

    version: Literal[1] = 1
    task_id: str
    capture_id: CaptureId
    input_digest: str
    plan_digest: str
    eligible: bool
    answer: str = Field(max_length=20000)
    test: TestResult | None
    queue: QueueState | None = None


def input_digest(task_id: str, state: dict[str, JsonValue], prompt: str) -> str:
    """Exclude changing process/session/output fields while binding original authority."""
    record = {key: state.get(key) for key in INPUTS}
    content = json.dumps([task_id, record, prompt], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode()).hexdigest()


def save(directory: Path, receipt: WritebackReceipt) -> None:
    """Publish a complete, synced receipt without replacing an earlier capture."""
    publish(directory, Path("writeback-receipt.json"), receipt.model_dump_json())


def plan_digest(plan: WritebackPlan) -> str:
    """Bind the parsed plan, independent of JSON whitespace."""
    return hashlib.sha256(plan.model_dump_json().encode()).hexdigest()


def load(
    directory: Path, expected_input: str, task_id: str, capture_id: str, names: tuple[str, ...]
) -> tuple[WritebackReceipt, WritebackPlan]:
    """Reject incomplete, foreign or changed evidence before any recovery effect."""
    receipt = WritebackReceipt.model_validate_json(
        read_metadata(owned_path(directory, Path("writeback-receipt.json")))
    )
    plan = WritebackPlan.model_validate_json(
        read_metadata(owned_path(directory, Path("writeback-plan.json")))
    )
    if (
        (receipt.task_id, receipt.capture_id, receipt.input_digest) !=
        (task_id, capture_id, expected_input)
        or receipt.plan_digest != plan_digest(plan)
        or tuple(item.name for item in plan.baseline) != names
        or tuple(item.name for item in plan.proposed) != names
        or len(names) != len(set(names))
    ):
        reason = "writeback_receipt_identity_changed"
        raise WritebackError(reason)
    if not receipt.eligible or (receipt.test is not None and receipt.test.exit_code != 0):
        reason = "writeback_test_not_passed"
        raise WritebackError(reason)
    return receipt, plan


def require_queue(current: QueueState | None, receipt: WritebackReceipt) -> QueueState:
    """Check the original invocation before any recovery file effect."""
    expected = receipt.queue
    if (
        expected is None or current is None
        or current.ticket != expected.ticket or current.delivery_id != expected.delivery_id
        or not current.delivery_id
        or current.phase not in {Phase.ARMED, Phase.STARTED, Phase.UNKNOWN, Phase.DONE}
    ):
        reason = "writeback_queue_identity_changed"
        raise WritebackError(reason)
    return current


def complete_queue(database: Connection, receipt: WritebackReceipt) -> None:
    """Retire only the same invocation after its caller proves file completion and exit."""
    current = require_queue(read(database, receipt.task_id), receipt)
    if current.phase == Phase.DONE:
        return
    result = QueueState(
        ticket=current.ticket, phase=Phase.DONE, delivery_id=current.delivery_id,
        owner=current.owner, acknowledged=current.acknowledged, launcher=current.launcher,
        reason="native_writeback_reconciled_without_model_call",
    )
    write(database, result, current)
