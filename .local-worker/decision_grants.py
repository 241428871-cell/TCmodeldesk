"""Consume human exceptions at the original stage claim and verify the exact executing task."""

from __future__ import annotations

from typing import TYPE_CHECKING

import control_bootstrap
import decision_scope
from plan_contracts import PlanError
from pydantic import ValidationError

if TYPE_CHECKING:
    from sqlite3 import Connection

    from model_desk_control.host_decision_scope import Subject
    from model_desk_control.host_decision_uses import Approval, DecisionUse


def _original(database: Connection, project: str, approval: Approval, current: Subject) -> None:
    """New cards carry independent stage subjects; historical cards keep whole-goal validity."""
    from model_desk_control.host_decision_scope import Authority, Subject

    stored = decision_scope.StoredSubject(
        approval.goal_id,
        approval.payload,
        approval.fingerprint,
        approval.subject_hash,
    )
    version = decision_scope.validate_subject(stored)
    if version.scope_version is None:
        expected = decision_scope.current_fingerprint(database, project, stored)
        original = decision_scope.RECORD.validate_json(approval.fingerprint)
        if original != expected:
            code = "review_legacy_stale"
            raise PlanError(code, "原整目标审核已失效，请主脑重新提交审核")
        return
    before = Subject.model_validate_json(approval.fingerprint)
    current_scope = current.stage_subjects[0]
    prior = next(
        (scope for scope in before.stage_subjects if scope.stage_id == current_scope.stage_id), None
    )
    if (
        prior is None
        or prior.decision_digest != current_scope.decision_digest
        or Authority.model_validate_json(before.model_dump_json())
        != Authority.model_validate_json(current.model_dump_json())
    ):
        code = "review_approval_stale"
        raise PlanError(code, "原审核的相关提案、产物或授权已变化，请主脑重新提交审核")


def _executing(
    database: Connection, project: str, approval: Approval, use: DecisionUse, *, reclaim: bool
) -> None:
    """Only the consumed task may change its output; its proposal and upstream inputs stay bound."""
    from model_desk_control.host_decision_scope import Authority
    from model_desk_control.host_decision_uses import assigned

    _ = decision_scope.validate_subject(
        decision_scope.StoredSubject(
            approval.goal_id,
            approval.payload,
            approval.fingerprint,
            approval.subject_hash,
        )
    )
    if (
        use.project != project
        or use.goal_id != approval.goal_id
        or use.subject_hash != approval.subject_hash
        or not assigned(database, use, allow_reclaim=reclaim)
    ):
        code = "review_use_identity"
        raise PlanError(code, "审核消费记录与当前原任务不一致，拒绝继续")
    current = decision_scope.branch_fingerprint(
        database,
        project,
        approval.goal_id,
        (use.stage_id,),
        executing=True,
    )
    if (
        current.stage_subjects[0].execution_digest != use.execution_digest
        or Authority.model_validate_json(current.model_dump_json()) != use.authority
    ):
        code = "review_execution_stale"
        raise PlanError(code, "已消费审核的提案、上游证据或授权已变化，拒绝旧任务继续")


def authorize(
    database: Connection,
    project: str,
    stage: str,
    task: str,
    *,
    consume: bool,
) -> None:
    """A prelaunch check cannot create a missing use or transfer one to another task."""
    control_bootstrap.bind_source()
    from model_desk_control.database import text_cursor
    from model_desk_control.host_decision_scope import Authority
    from model_desk_control.host_decision_uses import DecisionUse, approvals, read_use, save_use
    from model_desk_control.review_artifacts import ReviewScopeError

    owner = text_cursor(
        database, "SELECT goal_id,project FROM stages WHERE id=?", (stage,)
    ).fetchone()
    if owner is None or owner[1] != project:
        code = "review_stage_identity"
        raise PlanError(code, "审核所属阶段与当前项目不一致")
    current: Subject | None = None
    try:
        for approval in approvals(database, owner[0], stage):
            use = read_use(database, approval.id, stage)
            if use is not None:
                if use.task_id != task:
                    code = "review_already_consumed"
                    raise PlanError(code, "此审核已用于原任务；新的任务需要主脑重新提交审核")
                _executing(database, project, approval, use, reclaim=consume)
                continue
            if not consume:
                code = "review_use_missing"
                raise PlanError(code, "原任务缺少审核消费记录，不能在启动时补造授权")
            if current is None:
                current = decision_scope.branch_fingerprint(database, project, owner[0], (stage,))
            _original(database, project, approval, current)
            save_use(
                database,
                DecisionUse(
                    project=project,
                    goal_id=owner[0],
                    decision_id=approval.id,
                    stage_id=stage,
                    task_id=task,
                    subject_hash=approval.subject_hash,
                    authority=Authority.model_validate_json(current.model_dump_json()),
                    execution_digest=current.stage_subjects[0].execution_digest,
                ),
            )
    except (ReviewScopeError, ValidationError) as error:
        code = "review_use_unreadable"
        raise PlanError(code, "原审核证据或消费记录无法核对，请主脑处理后继续") from error
