"""File-only recovery through the original task, authority, queue and lease broker."""

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Literal

import continuation_store
import control_bootstrap
import execution_leases
import goal_events
import governance_context
import model_registry as registry
import stage_store
import task_queue
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

control_bootstrap.bind_source()
from model_desk_control import file_writeback, writeback_receipt
from model_desk_control.host_trace_models import CaptureId
from model_desk_control.native_output import owned_path, read_metadata

type Record = dict[str, JsonValue]


class RecoveryTask(BaseModel):
    """Use only a current project-owned native edit record for recovery."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    id: str
    project: str
    engine: Literal["claude_cli", "codex_cli"]
    kind: Literal["edit"]
    files: list[str] = Field(min_length=1)
    writeback_protocol: Literal[1]
    writeback_capture_id: CaptureId
    goal_id: str = ""
    stage_id: str = ""


def prepare(
    directory: Path, state: Record, plan: file_writeback.WritebackPlan,
    *, answer: str, test: Record | None,
) -> None:
    """Persist native success and test evidence before the first original file changes."""
    run, capture_id = directory.parent.parent, directory.name
    if directory.parent.name != "native-cli":
        reason = "writeback_capture_directory_invalid"
        raise file_writeback.WritebackError(reason)
    result = writeback_receipt.TestResult.model_validate(test) if test is not None else None
    eligible = (result is not None and result.exit_code == 0) or not state.get("test_command")
    queue = task_queue.observed(state) if task_queue.enabled(state) else None
    if task_queue.enabled(state) and queue is None:
        reason = "writeback_queue_receipt_missing"
        raise file_writeback.WritebackError(reason)
    receipt = writeback_receipt.WritebackReceipt(
        task_id=run.name, capture_id=capture_id,
        input_digest=writeback_receipt.input_digest(
            run.name, state, read_metadata(run / "prompt.txt")
        ),
        plan_digest=writeback_receipt.plan_digest(plan), eligible=eligible,
        answer=answer[-20000:], test=result, queue=queue,
    )
    writeback_receipt.save(directory, receipt)
    _ = registry.patch_task(
        run, writeback_protocol=1, writeback_capture_id=capture_id,
        writeback_state="prepared" if eligible else "test_failed",
    )


def _scope(run: Path, state: Record) -> RecoveryTask:
    if type(state.get("writeback_protocol")) is not int:
        reason = "writeback_protocol_invalid"
        raise file_writeback.WritebackError(reason)
    task = RecoveryTask.model_validate(state)
    root = Path(task.project)
    expected = owned_path(root, Path(".model-desk/runs") / task.id)
    if run != expected or str(root.resolve()) != task.project:
        reason = "writeback_project_identity_changed"
        raise file_writeback.WritebackError(reason)
    if task.stage_id:
        stage = stage_store.get_stage(task.stage_id, task.project)
        if (
            stage is None or stage.get("goal_id") != task.goal_id
            or stage.get("project") != task.project
            or stage.get("status") in {"accepted", "final_accepted", "cancelled"}
            or stage.get("active_task_id") not in {None, "", task.id}
        ):
            reason = "writeback_stage_identity_changed"
            raise file_writeback.WritebackError(reason)
    return task


def recover(run: Path, inactive: Callable[[Record], bool]) -> Record:
    """Recover saved files without launching a worker, model, test command or timer."""
    state = registry.read_task(run)
    _ = _scope(run, state)
    request = execution_leases.request_for(state, {"provider": "local", "id": "writeback"})
    token = state.get("execution_lease_token", "")
    if not isinstance(token, str) or (state.get("execution_lease_protocol") and not token):
        reason = "writeback_lease_identity_invalid"
        raise file_writeback.WritebackError(reason)
    with execution_leases.recovery(request, run, token):
        state = registry.read_task(run)
        with governance_context.launch_authority(state), registry.file_lock(run / "writeback.lock"):
            result = _recover_locked(run, state, inactive)
    goal_events.sync_task(registry.read_task(run))
    return result


def _recover_locked(run: Path, original: Record, inactive: Callable[[Record], bool]) -> Record:
    state = registry.read_task(run)
    task = _scope(run, state)
    if state != original or state.get("status") != "recovery_pending" or not inactive(state):
        reason = "writeback_original_worker_or_state_unconfirmed"
        raise file_writeback.WritebackError(reason)
    if (run / "cancelled.flag").exists():
        reason = "writeback_cancelled"
        raise file_writeback.WritebackError(reason)
    directory = owned_path(run, Path("native-cli") / task.writeback_capture_id)
    digest = writeback_receipt.input_digest(task.id, state, read_metadata(run / "prompt.txt"))
    receipt, plan = writeback_receipt.load(
        directory, digest, task.id, task.writeback_capture_id, tuple(task.files)
    )
    if state.get("test_command") and receipt.test is None:
        reason = "writeback_test_not_passed"
        raise file_writeback.WritebackError(reason)
    if task_queue.enabled(state):
        _ = writeback_receipt.require_queue(task_queue.observed(state), receipt)
    elif receipt.queue is not None:
        reason = "writeback_queue_protocol_changed"
        raise file_writeback.WritebackError(reason)

    def record_applied(applied: tuple[str, ...]) -> None:
        _ = registry.patch_task(run, writeback_recovered_files=list(applied))

    _ = file_writeback.apply(plan, Path(task.project), directory / "proposed-files", record_applied)
    # Include exact already-applied files; a crash may precede their task.json update.
    changed: list[JsonValue] = [
        old.name for old, new in zip(plan.baseline, plan.proposed, strict=True) if old != new
    ]
    if task_queue.enabled(state):
        with continuation_store.mutation(task.project, task.goal_id) as database:
            writeback_receipt.complete_queue(database, receipt)
    test = (
        TypeAdapter(Record).validate_json(receipt.test.model_dump_json()) if receipt.test else None
    )
    result = registry.patch_task(
        run, status="completed", phase="写回已恢复，待主脑独立验收", writeback_state="applied",
        changed_selected_files=changed, answer=receipt.answer, exit_code=0, error=None,
        ended_at=registry.now(), writeback_recovered_at=registry.now(),
        test=test,
    )
    goal_events.sync_task(result)
    return {"ok": True, "task_id": task.id, "status": "completed", "creates_new_record": False,
            "note": "已核对并恢复保存的文件成果；未调用模型，仍需独立验收。"}
