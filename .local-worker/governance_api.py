"""Project-scoped human control routes; getters never start a model or timer."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol, TypedDict
from pydantic import JsonValue

import continuation_clock
import dynamic_plan
import engineering_policy
import human_decisions
import goal_interactions
import goal_requirements
import project_views
import stage_store
import goal_file_scope
from plan_contracts import PlanError


class GovernanceError(ValueError):
    """A human control request is incomplete or does not apply to this project."""


class Panel(Protocol):
    """Human file revocation uses the existing identity-checked process cancellation."""

    def cancel_task(self, task_id: str, reason: str = '', actor: str = 'human') -> Mapping[str, JsonValue]: ...


class GovernanceSnapshot(TypedDict):
    project: str
    goals: list[dict]
    policy: dict
    decisions: list[dict]
    legacy_goal_count: int
    note: str


def project_for(view: dict, body: dict | None = None, *, required: bool = True) -> str:
    """A browser view fixes ownership; request bodies cannot select another project."""
    project = project_views.explicit_project(view) or ''
    if body and body.get('project') and body['project'] != project:
        raise PermissionError('请先切换到对应项目，再修改目标或工程习惯')
    if required and not project:
        raise GovernanceError('请先从左侧选择具体项目文件夹')
    return project


def snapshot(view: dict) -> GovernanceSnapshot:
    project = project_for(view, required=False)
    goals = []
    legacy_count = 0
    current_policy = engineering_policy.policy_snapshot(project)
    if project:
        for goal in stage_store.list_goals(project):
            if stage_store.record_store('goal', goal['id'], project) != project:
                legacy_count += 1
                continue
            plan = dynamic_plan.describe_goal(project, goal['id'])
            for stage in plan['stages']:
                stage['human_blockers'] = [{'id': item['id'], 'requires_replan': item.get('requires_replan', False)}
                    for item in human_decisions.pending_for_stage(project, stage['id'])]
            clock = continuation_clock.snapshot(project, goal['id'])
            goals.append({'goal': goal, 'plan': plan, 'clock': clock,
                          'policy_runtime': engineering_policy.goal_status(
                              project, goal['id'], current_policy['snapshot_id']),
                          'requirements': goal_requirements.list_for_goal(project, goal['id'])})
    return {'project': project, 'goals': goals,
            'policy': current_policy,
            'decisions': human_decisions.list_decisions(project) if project else [],
            'legacy_goal_count': legacy_count,
            'note': '读取状态不启动主脑；阶段由 AI 在人类固定准则内拆分和修订。'}


def get(path: str, view: dict) -> dict | None:
    progress = re.fullmatch(r'/api/governance/goals/([A-Za-z0-9_-]+)/progress', path)
    if progress:
        return goal_interactions.progress(project_for(view), progress[1])
    match = re.fullmatch(r'/api/governance/goals/([A-Za-z0-9_-]+)/recovery', path)
    if match:
        import goal_resolution
        return goal_resolution.document(goal_resolution.inspect_goal(project_for(view), match[1]))
    if path == '/api/governance':
        return snapshot(view)
    if path == '/api/engineering-policy':
        return engineering_policy.policy_snapshot(project_for(view, required=False))
    return None


def post(path: str, body: dict, view: dict, panel: Panel) -> dict | None:
    """Only the token-protected panel invokes this human answer/criteria boundary."""
    if path == '/api/engineering-policy':
        scope = body.get('scope')
        project_change = scope == 'project' or (scope is None and any(
            key in body for key in ('mode', 'rule_ids', 'custom_rules')))
        if not project_change:
            return engineering_policy.save_policy({**body, 'scope': 'global'})
        project = project_for(view, body)
        return engineering_policy.save_policy({**body, 'scope': 'project', 'project': project})
    if not path.startswith('/api/governance/'):
        return None
    import goal_controller
    project = project_for(view, body)
    recovery = re.fullmatch(r'/api/governance/goals/([A-Za-z0-9_-]+)/reconcile', path)
    if recovery:
        import goal_resolution
        return goal_resolution.document(goal_resolution.resolve_goal(project, recovery[1], body))
    interaction = re.fullmatch(r'/api/governance/goals/([A-Za-z0-9_-]+)/interactions', path)
    if interaction:
        message = str(body.get('message') or '')
        kind = str(body.get('kind') or 'requirement')
        if kind not in ('requirement', 'direction_change'):
            raise GovernanceError('需求类型必须是增量需求或方向调整')
        return goal_interactions.interact(
            project,
            interaction[1],
            message,
            kind=kind,
            request_id=str(body.get('request_id') or ''),
        )
    if path == '/api/governance/goals':
        from plan_contracts import GoalContract
        contract = GoalContract.model_validate({key: body[key] for key in (
            'criteria', 'review_policy', 'allowed_files', 'file_scope') if key in body})
        title = str(body.get('title') or '').strip()
        objective = str(body.get('objective') or '').strip()
        if not title or not objective:
            raise GovernanceError('请填写目标名称和目标内容')
        interval = body.get('interval_seconds', 1800)
        if not isinstance(interval, int) or isinstance(interval, bool) or not 60 <= interval <= 604800:
            raise GovernanceError('续跑周期应为 1 分钟到 7 天')
        goal = stage_store.create_goal(title, objective, project, actor='human', store='project')
        dynamic_plan.configure_goal(project, goal['id'], contract, actor='human')
        continuation_clock.configure_clock(project, goal['id'], interval_seconds=interval,
                                            enabled=False, obligations=body.get('obligations') or [])
        if body.get('start') is True:
            goal_controller.start_goal(project, goal['id'], body)
        return {'goal': goal, 'plan': dynamic_plan.describe_goal(project, goal['id']),
                'clock': continuation_clock.snapshot(project, goal['id'])}
    match = re.fullmatch(r'/api/governance/goals/([A-Za-z0-9_-]+)/(contract|control)', path)
    if match:
        goal_id, operation = match.groups()
        if stage_store.get_goal(goal_id, project) is None:
            raise GovernanceError('目标不存在或不属于当前项目')
        if operation == 'contract':
            updated = dynamic_plan.configure_goal(project, goal_id, {
                key: body[key] for key in ('criteria', 'review_policy', 'allowed_files', 'file_scope', 'base_revision') if key in body}, actor='human')
            stopped = []
            warnings = []
            for stage in stage_store.list_stages(goal_id, project):
                if stage.get('active_task_id') and stage.get('kind') == 'edit':
                    try:
                        goal_file_scope.validate_files(project, updated['file_scope'],
                            updated['allowed_files'], stage.get('files', []))
                    except PlanError as error:
                        revocation_reason = error.detail
                    else:
                        continue
                    try:
                        panel.cancel_task(stage['active_task_id'], reason='人类收窄目标文件授权: ' + revocation_reason, actor='human')
                        stopped.append(stage['active_task_id'])
                    except (ValueError, OSError):
                        warnings.append('任务 ' + stage['active_task_id'] + ' 未能确认停止，请核对执行记录')
            return {**updated, 'cancel_requested_tasks': stopped, 'warnings': warnings}
        action = body.get('action')
        if action == 'configure':
            state = continuation_clock.snapshot(project, goal_id)
            return continuation_clock.configure_clock(project, goal_id,
                interval_seconds=body.get('interval_seconds', state.get('interval_seconds', 1800)),
                enabled=state.get('enabled', False))
        if action in ('start', 'run_now'):
            return goal_controller.start_goal(project, goal_id, body)
        return goal_controller.control_goal(project, goal_id, action, body)
    match = re.fullmatch(r'/api/governance/decisions/([A-Za-z0-9_-]+)/answer', path)
    if match:
        return human_decisions.answer_decision(project, match[1], {
            key: body[key] for key in ('subject_hash', 'authority_epoch', 'option_id', 'notes') if key in body}, actor='human')
    raise GovernanceError('未知的目标控制操作')
