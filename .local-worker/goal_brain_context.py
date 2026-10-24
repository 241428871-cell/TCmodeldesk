"""Immutable planning inputs and native decision-model authorization."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import cli_worker
import collaboration_policy
import continuation_clock
import delegation_policy
import dynamic_plan
import engineering_policy
import execution_endpoints
import goal_requirements
import human_decisions
import model_registry as registry
import project_storage
import stage_store
import workflow_settings
import workspace_config
from goal_brain_contract import BrainError, Record

GUIDANCE_LIMIT_BYTES: Final = 65536
GUIDANCE_FILES: Final = ('AGENTS.md', 'CLAUDE.md')


@dataclass(frozen=True, slots=True)
class Selection:
    model_id: str
    engine: str
    model: str
    runtime: cli_worker.CliRuntime


def project_guidance(project: str) -> list[Record]:
    """Load only bounded project-owned instructions, with receipts of actual bytes."""
    root = Path(project).resolve()
    result = []
    remaining = GUIDANCE_LIMIT_BYTES
    for name in GUIDANCE_FILES:
        path = root / name
        if not path.resolve().is_relative_to(root):
            raise BrainError('项目准则文件链接指向项目外，已拒绝加载：' + name)
        if not path.exists():
            continue
        if not path.is_file():
            raise BrainError('项目准则路径不是文件：' + name)
        with path.open('rb') as source:
            raw = source.read(remaining + 1)
        if len(raw) > remaining:
            raise BrainError('项目根准则超过 64 KiB 总读取上限，请整理准则后再运行主脑')
        result.append({'path': name, 'sha256': hashlib.sha256(raw).hexdigest(), 'text': raw.decode('utf-8')})
        remaining -= len(raw)
    return result


def select_brain(project: str, model_id: str = '') -> Selection:
    """A goal can use only its enabled decision binding, never an implicit fallback."""
    project = project_storage.normalize(project)
    owner = workspace_config.project_config(project)
    selected = model_id
    if owner is not None and not selected:
        selected = next((row['model_id'] for row in owner['bindings']
                         if row['role'] == 'decision' and row['enabled']
                         and row.get('is_default')), '')
    binding = next((row for row in owner['bindings'] if row['model_id'] == selected
                    and row['role'] == 'decision' and row['enabled']), None) if owner else None
    if binding is None:
        raise BrainError('请先为此项目选择并启用一个默认决策模型，或明确选择项目决策模型')
    model = registry.get_model(selected)
    engine = str(model.get('provider') or '')
    if engine not in {'claude_cli', 'codex_cli'}:
        raise BrainError('自动主脑需要 Claude CLI 或 Codex CLI 决策模型；不会替换已选模型')
    settings = workflow_settings.snapshot()
    if not settings.get('enabled'):
        raise BrainError('协作总开关已关闭，主脑不会开始新的模型调用')
    execution_endpoints.gate(engine, model, settings, automatic=True)
    if (not settings.get('paid_allowed') or settings.get('enforce_paid_budget', True)
            or settings.get('enforce_model_call_limit', True)):
        raise BrainError('原生账户额度无法换算任务费用；需要现有付费授权，并明确关闭无法计量的金额与调用次数硬限制')
    runtime = {name: binding[name] for name in ('effort', 'fast', 'context_window', 'autocompact_tokens')}
    cli_worker.runtime_options(engine, runtime)
    return Selection(selected, engine, str(model['model']), runtime)


def build_context(
    project: str, goal_id: str, selection: Selection, *, requirements_through: int | None = None
) -> Record:
    goal = stage_store.get_goal(goal_id, project)
    if goal is None or goal['status'] != stage_store.GOAL_ACTIVE:
        raise BrainError('目标不存在或已经结束')
    contract = dynamic_plan.describe_goal(project, goal_id)
    tasks = []
    for stage in contract['stages']:
        for task_id in stage.get('task_ids', []):
            run = registry.task_dir(task_id)
            state = registry.read_task(run)
            if state.get('goal_id') != goal_id or state.get('project') != project:
                raise BrainError('阶段任务的项目或目标标识不一致，已阻止跨项目读取')
            review = delegation_policy.load(run)
            tasks.append({'id': task_id, 'stage_id': stage['id'], 'status': state.get('status'),
                          'files': state.get('files', []),
                          'changed_files': state.get('changed_selected_files', state.get('changed_files', [])),
                          'answer': str(state.get('answer') or '')[-20000:],
                          'test': state.get('test'), 'writeback_state': state.get('writeback_state'),
                          'review': review})
    return {'project': project, 'goal_id': goal_id, 'title': goal['title'], 'objective': goal['objective'],
            'contract': contract, 'tasks': tasks, 'decisions': human_decisions.list_decisions(project, goal_id),
            'clock': continuation_clock.snapshot(project, goal_id),
            'latest_requirements': goal_requirements.list_for_goal(
                project, goal_id, through=requirements_through
            ),
            'execution_models': workspace_config.execution_models(project, registry.list_models()['models']),
            'project_guidance': project_guidance(project),
            'engineering_policy': engineering_policy.task_context(project),
            'collaboration': collaboration_policy.context(project),
            'brain': {'model_id': selection.model_id, 'engine': selection.engine, 'runtime': selection.runtime}}


def receipt_directory(project: str, goal_id: str, intent_id: str) -> Path:
    if any(re.fullmatch(r'[A-Za-z0-9_-]{1,100}', part) is None for part in (goal_id, intent_id)):
        raise BrainError('目标或续跑标识无效')
    root = Path(project).resolve()
    directory = root / '.model-desk' / 'goal-brain' / goal_id / intent_id
    if directory.exists() or not directory.resolve().is_relative_to(root):
        raise BrainError('主脑本轮记录已存在或目录不属于当前项目，不会重复执行')
    return directory
