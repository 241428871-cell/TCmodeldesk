"""Adapt branch subjects to existing human questions without widening historical answers."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final, Literal, assert_never

import continuation_store
import control_bootstrap
import plan_records as records
from plan_contracts import DecisionRequest, PlanError
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

if TYPE_CHECKING:
    from collections.abc import Generator
    from sqlite3 import Connection

    from model_desk_control.host_decision_scope import Subject

type Record = dict[str, JsonValue]
RECORD: Final = TypeAdapter(Record)
MAX_SUBJECT_BYTES: Final = 262144


class SavedFingerprint(BaseModel):
    """Only an explicit version 2 record receives branch semantics."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    scope_version: Literal[2] | None = None


class SavedBranch(BaseModel):
    """Stored option IDs differ from a new request; only authoritative scope is parsed here."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    kind: Literal["exception", "final"]
    affected_stage_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StoredSubject:
    """The original SQL owner and unchanged proposal are verified together."""

    goal_id: str
    payload: str
    fingerprint: str
    subject_hash: str


def validate_subject(stored: StoredSubject) -> SavedFingerprint:
    """Bound saved JSON and verify its unchanged proposal before legacy row decoding."""
    if len(stored.payload.encode()) + len(stored.fingerprint.encode()) > MAX_SUBJECT_BYTES:
        code = "review_subject_budget"
        raise PlanError(code, "原审核记录超过读取预算，无法核对，请主脑缩小证据后重新提交")
    try:
        proposal = RECORD.validate_json(stored.payload)
        original = RECORD.validate_json(stored.fingerprint)
        version = SavedFingerprint.model_validate(original)
    except ValidationError as error:
        code = "review_subject_unreadable"
        raise PlanError(code, "原审核记录无法核对，请主脑重新提交审核") from error
    expected = records.digest(
        {
            "goal_id": stored.goal_id,
            "proposal": proposal,
            "fingerprint": original,
        }
    )
    if expected != stored.subject_hash:
        code = "review_subject_corrupt"
        raise PlanError(code, "原审核内容与指纹不一致，请主脑重新提交审核")
    return version


def branch_fingerprint(
    database: Connection,
    project: str,
    goal: str,
    selected: tuple[str, ...],
    *,
    executing: bool = False,
) -> Subject:
    """Observe a pending subject, or one consumed stage's immutable proposal and upstream inputs."""
    control_bootstrap.bind_source()
    from model_desk_control.host_decision_scope import Authority, ReviewRequest, fingerprint
    from model_desk_control.review_artifacts import ReviewScopeError

    try:
        authority = Authority.model_validate(records.fingerprint(database, project, goal))
        request = ReviewRequest(goal_id=goal, selected=selected, for_execution=executing)
        return fingerprint(database, Path(project), request, authority)
    except (ReviewScopeError, ValidationError) as error:
        code = "review_scope_unreadable"
        raise PlanError(
            code,
            "受影响阶段、依赖或文件证据缺失、已变化或超过核对预算，请主脑重新核对审核范围",
        ) from error


def create_fingerprint(
    database: Connection, project: str, goal: str, request: DecisionRequest
) -> Record:
    """Final approval remains whole-goal; new exceptions bind their actual upstream branch."""
    import decision_records

    match request.kind:
        case "final":
            return decision_records.current_fingerprint(database, project, goal)
        case "exception":
            subject = branch_fingerprint(database, project, goal, tuple(request.affected_stage_ids))
            return RECORD.validate_json(subject.model_dump_json())
        case _:
            assert_never(request.kind)


def current_fingerprint(database: Connection, project: str, stored: StoredSubject) -> Record:
    """Verify the saved proposal, then preserve legacy or explicitly versioned scope."""
    import decision_records

    goal, payload = stored.goal_id, stored.payload
    version = validate_subject(stored)
    if version.scope_version is None:
        return decision_records.current_fingerprint(database, project, goal)
    try:
        scope = SavedBranch.model_validate_json(payload)
    except ValidationError as error:
        code = "review_scope_unreadable"
        raise PlanError(code, "原审核范围无法核对，请主脑重新提交审核") from error
    match scope.kind:
        case "exception":
            subject = branch_fingerprint(database, project, goal, scope.affected_stage_ids)
            return RECORD.validate_json(subject.model_dump_json())
        case "final":
            code = "review_scope_invalid"
            raise PlanError(code, "最终审核不能使用分支范围，请主脑重新提交")
        case _:
            assert_never(scope.kind)


@contextmanager
def answering(project: str, decision_id: str) -> Generator[str]:
    """Serialize with the existing goal's controlled file writeback, without a new lock domain."""
    control_bootstrap.bind_source()
    from model_desk_control.database import text_cursor

    with continuation_store.readonly(project) as database:
        if database is None or not records.has_table(database, "human_decisions"):
            code = "decision_missing"
            raise PlanError(code, "当前项目中不存在该审核项")
        row = text_cursor(
            database, "SELECT goal_id FROM human_decisions WHERE id=?", (decision_id,)
        ).fetchone()
        if row is None:
            code = "decision_missing"
            raise PlanError(code, "当前项目中不存在该审核项")
        goal = row[0]
    _ = records.owner(project, goal)
    with continuation_store.locked(project, goal):
        yield goal
