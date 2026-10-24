"""Immutable applied guidance and current, revocable dispatch authority."""
from __future__ import annotations

import engineering_policy
import goal_file_scope
from plan_contracts import PlanError
from functools import wraps
from copy import deepcopy
from contextlib import contextmanager
import subprocess
from typing import TypedDict


class DispatchBlocked(ValueError):
    """Live human authority currently refuses this goal effect."""


class AppliedPolicy(TypedDict):
    prompt: str
    snapshot: dict
    snapshot_id: str


class DispatchAuthority(TypedDict, total=False):
    allowed: bool
    reason: str
    authority_epoch: int
    exists: bool
    goal_id: str
    stage_id: str
    contract_revision: int
    contract: dict


def criteria_prompt(project: str, goal_id: str) -> str:
    """Include fixed criteria before routing estimates and native context checks."""
    if not project or not goal_id:
        return ''
    import plan_records
    import stage_store
    with stage_store.connection(project) as conn:
        contract = plan_records.contract(conn, goal_id)
    if not contract['revision']:
        return ''
    return '人类固定准则（不能通过执行计划或旧快照降低）：\n' + '\n'.join(
        '• ' + item for item in contract['criteria'])


def task_policy(project: str, parent: dict | None = None) -> AppliedPolicy:
    if parent and isinstance(parent.get('engineering_policy'), dict):
        return deepcopy(parent['engineering_policy'])
    return engineering_policy.task_context(project)


def require_task_requirements_current(state: dict) -> None:
    """Refuse effects from a managed brain task whose requirement snapshot is stale."""
    import continuation_clock

    round_id = state.get('goal_round_id')
    if not round_id:
        return
    revision = state.get('goal_requirements_revision')
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise DispatchBlocked('原生目标任务缺少可信的运行中需求版本，旧副作用未应用')
    continuation_clock.require_task_requirements_current(
        str(state.get('project') or ''), str(state.get('goal_id') or ''), str(round_id),
        int(state.get('authority_epoch') or 0), revision,
    )


def guard(project: str, goal_id: str, stage_id: str = '', authority_epoch: int | None = None,
          files: list[str] | None = None, kind: str | None = None,
          contract_revision: int | None = None, require_contract_revision: bool = False,
          automatic_goal: bool = False, *, task_id: str = '') -> DispatchAuthority:
    if not goal_id:
        return {}
    import continuation_clock
    import human_decisions
    import plan_records
    import stage_store
    result = continuation_clock.dispatch_guard(project, goal_id, stage_id, authority_epoch=authority_epoch)
    if not result['allowed']:
        raise DispatchBlocked(result['reason'])
    if result['exists'] or automatic_goal:
        import workflow_settings
        settings = workflow_settings.snapshot()
        if not result['exists']:
            raise DispatchBlocked('自动目标的续跑授权记录缺失，请先核对原目标')
        if not settings.get('enabled') or not settings.get('readable', True):
            raise DispatchBlocked('协作总开关已关闭，自动目标不会派发或启动新的工作')
    with stage_store.connection(project) as conn:
        contract = plan_records.contract(conn, goal_id)
        if stage_id and task_id:
            import decision_grants
            decision_grants.authorize(conn, project, stage_id, task_id, consume=False)
    if contract['revision']:
        if ((contract_revision is not None or require_contract_revision) and
                contract_revision != contract['revision']):
            raise DispatchBlocked('人类准则已更新，旧排队任务需要主脑按新准则重新规划')
        stage = stage_store.get_stage(stage_id, project) if stage_id else None
        selected_files = files if files is not None else (stage or {}).get('files', [])
        task_kind = kind or (stage or {}).get('kind')
        if task_kind == 'edit':
            try:
                goal_file_scope.validate_files(project, contract['file_scope'], contract['allowed_files'], selected_files)
            except PlanError as error:
                raise DispatchBlocked('当前人类文件授权已收窄或目标已不合法，原阶段需重新规划: ' + error.detail) from error
    pending = human_decisions.pending_for_stage(project, stage_id) if stage_id else []
    if pending:
        raise DispatchBlocked('此阶段等待人类决策：' + '；'.join(str(row.get('title') or row['id']) for row in pending))
    return {**result, 'contract_revision': contract['revision'], 'contract': contract}


@contextmanager
def launch_authority(state: dict):
    """Recheck at the actual process boundary; release before waiting for the worker."""
    import continuation_clock
    project, goal_id = state.get('project') or '', state.get('goal_id') or ''
    if project and goal_id:
        with continuation_clock.authority_lock(project, goal_id, state.get('authority_epoch')):
            guard(project, goal_id, state.get('stage_id') or '', state.get('authority_epoch'),
                  state.get('files'), state.get('kind'), state.get('goal_contract_revision'), True,
                  state.get('automatic_goal', False), task_id=state.get('id') or '')
            require_task_requirements_current(state)
            yield
    else:
        yield


def fenced_popen(run, state: dict, argv: list[str], **options):
    """Start a DSH/Pi worker process under the same live authority as native CLI launches."""
    with launch_authority(state):
        if (run / 'cancelled.flag').exists():
            raise DispatchBlocked('任务已取消，未启动执行进程')
        return subprocess.Popen(argv, **options)  # noqa: S603 - argv is built by the worker.


def fenced_dispatch(function):
    """Keep a discrete stage dispatch atomic with human revocation, without waiting on workers."""
    @wraps(function)
    def invoke(body, *args, **kwargs):
        import continuation_clock
        import stage_store
        import model_registry as registry
        project, goal_id = body.get('project') or '', body.get('goal_id') or ''
        if body.get('stage_id'):
            stage = stage_store.get_stage(body['stage_id'], project or None)
            if stage:
                project, goal_id = stage.get('project') or project, stage['goal_id']
        elif body.get('continue_task') or body.get('rework_of'):
            parent = registry.read_task(registry.task_dir(body.get('continue_task') or body['rework_of'], project or None))
            project, goal_id = parent.get('project') or project, parent.get('goal_id') or goal_id
        if project and goal_id:
            with continuation_clock.authority_lock(project, goal_id, body.get('authority_epoch')):
                return function(body, *args, **kwargs)
        return function(body, *args, **kwargs)
    return invoke
