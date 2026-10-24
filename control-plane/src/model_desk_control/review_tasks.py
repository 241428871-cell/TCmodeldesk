"""Hash the real, project-owned task evidence of a reviewed dependency branch."""

import json
from hashlib import sha256
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, JsonValue, TypeAdapter, ValidationError

from model_desk_control.host_flow import HostRecord
from model_desk_control.host_trace_models import LegacyObservation, RecordId
from model_desk_control.native_output import (
    METADATA_BYTES,
    NativeReadError,
    owned_path,
    read_metadata,
)
from model_desk_control.review_artifacts import ReviewScopeError

MAX_TASKS: Final = 200
TOTAL_BYTES: Final = 2 * 1024 * 1024
RECORD: Final = TypeAdapter(dict[str, JsonValue])


class TaskBinding(HostRecord):
    """Every associated task belongs to one original stage of this goal."""

    task_id: RecordId
    stage_id: RecordId


class SavedTask(LegacyObservation):
    """A staged task cannot omit its goal and stage identity."""

    id: RecordId
    project: str
    goal_id: RecordId
    stage_id: RecordId
    status: str


class SavedReview(LegacyObservation):
    """Result records are observations, never independent acceptance grants."""

    schema_version: Literal[1] = Field(alias="schema")
    task_id: RecordId
    goal_id: RecordId
    stage_id: RecordId
    project: str | None = None


class RecordDigest(HostRecord):
    """No task text enters a question's public fingerprint."""

    path: str
    present: bool
    digest: str


class TaskEvidence(HostRecord):
    """Missing optional results and their later appearance are distinct evidence."""

    records: tuple[RecordDigest, ...]

    @property
    def digest(self) -> str:
        """Bind a deterministic sequence of canonical JSON content digests."""
        return sha256(self.model_dump_json().encode()).hexdigest()


def _record(project: Path, relative: Path, remaining: int) -> str | None:
    path = owned_path(project, relative)
    try:
        return read_metadata(path, byte_limit=min(remaining, METADATA_BYTES))
    except FileNotFoundError:
        return None


def _task(project: Path, goal: str, binding: TaskBinding, raw: str) -> None:
    saved = SavedTask.model_validate_json(raw)
    if (
        saved.id != binding.task_id
        or saved.project != str(project)
        or saved.goal_id != goal
        or saved.stage_id != binding.stage_id
    ):
        code = "review_task_identity"
        raise ReviewScopeError(code)


def _result(project: Path, goal: str, binding: TaskBinding, name: str, raw: str) -> None:
    saved = SavedReview.model_validate_json(raw)
    if (
        saved.task_id != binding.task_id
        or saved.goal_id != goal
        or saved.stage_id != binding.stage_id
        or saved.project not in {None, str(project)}
        or (name == "handover.json" and saved.project != str(project))
    ):
        code = "review_result_identity"
        raise ReviewScopeError(code)


def observe(project: Path, goal: str, bindings: tuple[TaskBinding, ...]) -> TaskEvidence:
    """Read only bounded fixed task paths, without registry fallback or process activity."""
    if len(bindings) > MAX_TASKS or len({item.task_id for item in bindings}) != len(bindings):
        code = "review_task_associations"
        raise ReviewScopeError(code)
    records: list[RecordDigest] = []
    remaining = TOTAL_BYTES
    try:
        for binding in sorted(bindings, key=lambda item: (item.stage_id, item.task_id)):
            for name in ("task.json", "review.json", "handover.json"):
                relative = Path(".model-desk/runs") / binding.task_id / name
                raw = _record(project, relative, remaining)
                if raw is None:
                    if name == "task.json":
                        code = "review_task_missing"
                        raise ReviewScopeError(code)
                    records.append(RecordDigest(path=relative.as_posix(), present=False, digest=""))
                    continue
                remaining -= len(raw.encode())
                if name == "task.json":
                    _task(project, goal, binding, raw)
                else:
                    _result(project, goal, binding, name, raw)
                content = RECORD.validate_json(raw)
                canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
                records.append(
                    RecordDigest(
                        path=relative.as_posix(),
                        present=True,
                        digest=sha256(canonical.encode()).hexdigest(),
                    )
                )
        return TaskEvidence(records=tuple(records))
    except (OSError, NativeReadError, ValidationError, UnicodeError) as error:
        code = "review_task_unreadable"
        raise ReviewScopeError(code) from error
