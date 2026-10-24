"""Token-protected human lifecycle disposition, separate from brain tools and acceptance."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import continuation_clock
import continuation_store
import control_bootstrap
import goal_brain_runner
import model_registry as registry
from pydantic import JsonValue, TypeAdapter

if TYPE_CHECKING:
    from collections.abc import Generator

    from model_desk_control.host_resolution_models import ResolutionResult, ResolutionView

type Record = dict[str, JsonValue]
RECORD: Final = TypeAdapter(Record)
ERRORS: Final = {
    "resolution_project_mismatch": "核对对象不属于当前项目，请重新选择项目。",
    "resolution_no_original_round": "此目标已没有待核对的原轮次，请刷新列表。",
    "resolution_clock_unreadable": "目标时钟记录不完整或超过读取上限，未结束原轮次。",
    "resolution_original_identity_conflict": "原轮次身份记录不一致，未执行处理。",
    "resolution_completed_input_conflict": "此事项已按另一份依据处理，不能用新内容覆盖原决定。",
    "resolution_evidence_changed": "原任务、授权或回执已变化，请刷新并重新核对。",
    "resolution_incomplete_evidence": "仍有工作者在运行或证据缺失，请先处理清单中的阻塞项。",
    "resolution_notes_required": "请填写实际核对的依据。",
}


class ReconciliationError(ValueError):
    """A stable domain reason is translated without exposing model output or secrets."""

    def __init__(self, code: str) -> None:
        """Keep the original code available for diagnostics."""
        self.code: str = code
        super().__init__(ERRORS.get(code, "原轮次暂不能处理，请核对当前证据。"))


@contextmanager
def _errors() -> Generator[None]:
    control_bootstrap.bind_source()
    from model_desk_control.host_resolution_models import ResolutionError

    try:
        yield
    except ResolutionError as error:
        raise ReconciliationError(error.code) from error


def _probe(pid: int, birth: float) -> Literal["running", "exited", "unknown"]:
    return goal_brain_runner.inspect_owned(pid, birth)[0]


def inspect_goal(project: str, goal_id: str) -> ResolutionView:
    """Opening the human checklist does not mutate a clock, task or phase."""
    with _errors(), continuation_store.readonly(project) as database:
        from model_desk_control.host_resolution import inspect
        from model_desk_control.host_resolution_models import ResolutionError

        if database is None:
            code = "resolution_no_original_round"
            raise ResolutionError(code)
        return inspect(database, Path(project).resolve(strict=True), goal_id, _probe)


def resolve_goal(project: str, goal_id: str, payload: Record) -> ResolutionResult:
    """The graph lock precedes the goal lock, matching the actual round driver."""
    with _errors():
        from model_desk_control.host_resolution import resolve
        from model_desk_control.host_resolution_models import (
            ResolutionError,
            ResolutionInput,
            ResolutionResult,
        )
        from model_desk_control.host_resolution_records import load, reference

        request = ResolutionInput.model_validate(payload)
        root = Path(project).resolve(strict=True)
        with continuation_store.readonly(project) as database:
            if database is None:
                code = "resolution_no_original_round"
                raise ResolutionError(code)
            scope = reference(load(database, root, goal_id, request.intent_id), root)
        identity = sha256(scope.model_dump_json().encode()).hexdigest()
        with (
            registry.file_lock(Path(registry.HERE) / ("goal-graph-" + identity + ".lock")),
            continuation_store.mutation(project, goal_id) as database,
        ):
            outcome = resolve(database, root, goal_id, request, _probe)
        clock = RECORD.validate_python(continuation_clock.snapshot(project, goal_id))
        return ResolutionResult(receipt=outcome.receipt, replayed=outcome.replayed, clock=clock)


def document(value: ResolutionView | ResolutionResult) -> Record:
    """Keep the existing panel JSON boundary explicitly validated."""
    return RECORD.validate_json(value.model_dump_json())
