"""Read-only presentation of project bindings and actual controller activity."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import enforcement_report
import execution_endpoints
import execution_engines
import model_agent_defaults
import workflow_settings

if TYPE_CHECKING:
    from workspace_config import Runtime, Store


class WorkspaceSnapshot(TypedDict):
    schema: int
    selected_project: str | None
    hidden_projects: list[str]
    projects: list[dict]
    defaults: dict
    readable: bool
    error: str


def snapshot(store: Store, models: list[dict], tasks: list[dict], defaults: Runtime) -> WorkspaceSnapshot:
    """Availability never pretends that an enabled CLI already has a process."""
    import execution_leases

    waiting = dict(execution_leases.state().waiting)
    indexed = {m['id']: m for m in models}
    projects = []
    agents = execution_engines.public_agents()
    controls = execution_endpoints.snapshot()
    settings = workflow_settings.snapshot()
    for saved in store['projects']:
        brain_activity = _decision_activity(saved['path'])
        project = {**saved, 'bindings': [], 'selected': saved['path'] == store['selected_project'],
                   'available': Path(saved['path']).is_dir()}
        project['waiting_tasks'] = [
            {'task_id': task['id'], 'title': task.get('title') or task['id'],
             'reason': waiting[task['id']]}
            for task in tasks if task.get('project') == saved['path'] and task.get('id') in waiting]
        for binding in saved['bindings']:
            model = indexed.get(binding['model_id'])
            engine = ''
            try:
                engine = model_agent_defaults.effective(model, settings)[0] if model else ''
                runtime_ready = bool(agents.get(engine, {}).get('ready'))
                endpoint_enabled = bool(controls['readable'] and controls['endpoints'].get(engine, {}).get('enabled'))
                blocked = ('' if endpoint_enabled else '执行端尚未启用')
            except ValueError as error:
                runtime_ready, endpoint_enabled, blocked = False, False, str(error)
            ready = bool(model and model.get('enabled', True) and model.get('account_enabled', True)
                         and runtime_ready and endpoint_enabled)
            if model and model.get('account_enabled') is False:
                blocked = '此模型引用的账户已停用'
            activity = brain_activity.get(binding['model_id'], {}) if binding['role'] == 'decision' else {}
            count = (int(activity.get('running', 0)) if binding['role'] == 'decision' else
                     sum(t.get('project') == saved['path'] and t.get('model_id') == binding['model_id']
                         and t.get('status') == 'running' for t in tasks))
            starting = int(activity.get('starting', 0))
            unknown = int(activity.get('unknown', 0))
            status = ('running' if count else 'unknown' if unknown else 'starting' if starting
                      else 'disabled' if not binding['enabled'] else 'ready' if ready else 'unavailable')
            project['bindings'].append({**binding, 'model': model, 'ready': ready, 'running_count': count,
                'starting_count': starting, 'unknown_count': unknown,
                'status': status, 'runtime_ready': runtime_ready, 'endpoint_enabled': endpoint_enabled,
                'enforcement': enforcement_report.describe(
                    str(model.get('provider') or '') if binding['role'] == 'decision' and model else engine,
                    binding['role']),
                'blocked_reason': blocked or ('' if runtime_ready else '本机执行器未就绪'),
                'runtime_note': '上下文是可配置预算，不是已核实的服务商最大窗口；Claude 自动压缩为窗口，实际触发点由 CLI 保留缓冲决定。配置用于新任务，启用不代表正在推理'})
        projects.append(project)
    return {**store, 'projects': projects, 'defaults': {**defaults, 'autocompact_ratio': .8},
            'readable': True, 'error': ''}


def _decision_activity(project: str) -> dict[str, dict[str, int]]:
    """Count durable brain launch states without starting or probing a CLI."""
    import continuation_clock
    import project_storage
    import stage_store

    activity: dict[str, dict[str, int]] = {}
    if not project_storage.records_db(project).is_file():
        return activity
    try:
        goals = stage_store.list_goals(project)
    except (OSError, ValueError):
        return activity
    for goal in goals:
        try:
            clock = continuation_clock.snapshot(project, str(goal['id']))
        except (KeyError, OSError, ValueError):
            continue
        intent = clock.get('active_intent')
        if not isinstance(intent, dict):
            continue
        model_id = str(intent.get('brain_model_id') or clock.get('brain_model_id') or '')
        if not model_id:
            continue
        counts = activity.setdefault(model_id, {'running': 0, 'starting': 0, 'unknown': 0})
        state = str(intent.get('status') or '')
        if state == 'started':
            counts['running'] += 1
        elif state == 'claimed':
            counts['starting'] += 1
        else:
            counts['unknown'] += 1
    return activity
