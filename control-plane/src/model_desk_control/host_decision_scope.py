"""Branch review subjects over the original stage database, without a second authority."""

from graphlib import CycleError, TopologicalSorter
from hashlib import sha256
from pathlib import Path
from sqlite3 import Connection
from typing import Annotated, ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, Json, ValidationError

from model_desk_control.database import text_cursor
from model_desk_control.host_flow import HostRecord
from model_desk_control.host_trace_models import CaptureId, RecordId
from model_desk_control.review_artifacts import ReviewScopeError, observe
from model_desk_control.review_scope_parts import StagePart, StageSubject, subjects
from model_desk_control.review_tasks import TaskBinding
from model_desk_control.review_tasks import observe as observe_tasks

MAX_STAGES: Final = 200
MAX_METADATA: Final = 2 * 1024 * 1024
COLUMNS: Final = (
    "id",
    "goal_id",
    "project",
    "title",
    "objective",
    "kind",
    "files",
    "acceptance",
    "candidates",
    "fixed_model",
    "depends_on",
    "session_policy",
    "status",
    "task_ids",
    "active_task_id",
    "data",
)
STAGE_QUERY: Final = """SELECT id,goal_id,project,title,objective,kind,files,acceptance,
candidates,fixed_model,depends_on,session_policy,status,task_ids,active_task_id,data
FROM stages WHERE id=? AND goal_id=? AND length(CAST(
id||goal_id||project||title||objective||kind||files||acceptance||candidates||fixed_model||
depends_on||session_policy||status||task_ids||active_task_id||data AS BLOB))<=?"""


class SavedRecord(BaseModel):
    """Read a finite schema-owned view of legacy JSON; unrelated fields confer no authority."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")


class Dynamic(SavedRecord):
    """A whole-plan revision is deliberately not part of a branch's semantic identity."""

    key: str = ""
    superseded_by: int = 0


class Metadata(SavedRecord):
    """Only stable key and retirement identity matter, not a whole-plan save timestamp."""

    dynamic_plan: Dynamic = Dynamic()


class Stage(SavedRecord):
    """A bounded original-stage observation includes proposal and relevant lifecycle facts."""

    id: RecordId
    goal_id: RecordId
    project: str
    title: str
    objective: str
    kind: Literal["chat", "edit"]
    files: Json[Annotated[tuple[str, ...], Field(max_length=30)]]
    acceptance: Json[Annotated[tuple[str, ...], Field(max_length=40)]]
    candidates: Json[Annotated[tuple[str, ...], Field(max_length=40)]]
    fixed_model: str
    depends_on: Json[Annotated[tuple[RecordId, ...], Field(max_length=40)]]
    session_policy: str
    status: str
    task_ids: Json[Annotated[tuple[RecordId, ...], Field(max_length=200)]]
    active_task_id: CaptureId
    data: Json[Metadata]


class Authority(SavedRecord):
    """All human criteria, policy and continuation authority still apply to every branch."""

    authority_epoch: int
    policy_revision: int
    policy_snapshot_id: str
    stopped: bool
    contract_revision: int
    criteria_digest: str


class Subject(Authority):
    """The browser sees hashes; output and task text are never copied into this fingerprint."""

    scope_version: Literal[2] = 2
    affected_stage_ids: tuple[str, ...]
    branch_digest: str
    artifact_digest: str
    task_digest: str
    stage_subjects: tuple[StageSubject, ...]


class ReviewRequest(HostRecord):
    """Execution checks exclude only the one consumed stage's mutable outputs and task history."""

    goal_id: RecordId
    selected: tuple[RecordId, ...] = Field(min_length=1, max_length=200)
    for_execution: bool = False


class Branch(HostRecord):
    """A deterministic ordered dependency closure is independent of unrelated branches."""

    stages: tuple[Stage, ...]


def _stage(database: Connection, goal: str, stage: str, remaining: int) -> tuple[Stage, int]:
    row = text_cursor(
        database,
        STAGE_QUERY,
        (stage, goal, remaining),
    ).fetchone()
    if row is None:
        code = "review_stage_missing_or_budget"
        raise ReviewScopeError(code)
    try:
        return Stage.model_validate(dict(zip(COLUMNS, row, strict=True))), sum(
            len(column.encode()) for column in row
        )
    except ValidationError as error:
        code = "review_stage_unreadable"
        raise ReviewScopeError(code) from error


def fingerprint(
    database: Connection, project: Path, request: ReviewRequest, authority: Authority
) -> Subject:
    """Bind a question to affected stages plus their full upstream dependency closure."""
    selected, goal = request.selected, request.goal_id
    if request.for_execution and len(selected) != 1:
        code = "review_execution_scope"
        raise ReviewScopeError(code)
    pending = sorted(set(selected))
    saved: dict[str, Stage] = {}
    remaining = MAX_METADATA
    while pending:
        stage_id = pending.pop()
        if stage_id in saved:
            continue
        if len(saved) >= MAX_STAGES:
            code = "review_branch_budget"
            raise ReviewScopeError(code)
        stage, used = _stage(database, goal, stage_id, remaining)
        remaining -= used
        if stage.project != str(project) or stage.data.dynamic_plan.superseded_by or remaining < 0:
            code = "review_branch_identity"
            raise ReviewScopeError(code)
        saved[stage_id] = stage
        pending.extend(stage.depends_on)
    branch = Branch(stages=tuple(saved[key] for key in sorted(saved)))
    try:
        _ = tuple(
            TopologicalSorter(
                {key: stage.depends_on for key, stage in saved.items()}
            ).static_order()
        )
    except CycleError as error:
        code = "review_branch_cycle"
        raise ReviewScopeError(code) from error
    bindings = tuple(
        TaskBinding(task_id=task_id, stage_id=stage.id)
        for stage in branch.stages
        if not request.for_execution or stage.id not in selected
        for task_id in sorted(
            set(stage.task_ids) | ({stage.active_task_id} if stage.active_task_id else set())
        )
    )
    tasks = observe_tasks(project, goal, bindings)
    files = tuple(
        path
        for stage in branch.stages
        if not request.for_execution or stage.id not in selected
        for path in stage.files
    )
    artifacts = observe(project, files)
    parts = tuple(
        StagePart(
            stage_id=stage.id,
            dependencies=stage.depends_on,
            files=stage.files,
            task_ids=tuple(
                sorted(
                    set(stage.task_ids)
                    | ({stage.active_task_id} if stage.active_task_id else set())
                )
            ),
            full_digest=sha256(stage.model_dump_json().encode()).hexdigest(),
            proposal_digest=sha256(
                stage.model_copy(
                    update={
                        "status": "",
                        "task_ids": (),
                        "active_task_id": "",
                    }
                )
                .model_dump_json()
                .encode()
            ).hexdigest(),
        )
        for stage in branch.stages
    )
    return Subject(
        authority_epoch=authority.authority_epoch,
        policy_revision=authority.policy_revision,
        policy_snapshot_id=authority.policy_snapshot_id,
        stopped=authority.stopped,
        contract_revision=authority.contract_revision,
        criteria_digest=authority.criteria_digest,
        affected_stage_ids=tuple(sorted(set(selected))),
        branch_digest=sha256(branch.model_dump_json().encode()).hexdigest(),
        artifact_digest=artifacts.digest,
        task_digest=tasks.digest,
        stage_subjects=subjects(parts, artifacts, tasks, selected, execution=request.for_execution),
    )
