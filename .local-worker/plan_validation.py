"""Plan normalization, immutable execution history and dependency validation."""
from __future__ import annotations

import json
from typing import Final, Literal, TypedDict
from uuid import uuid4

from pydantic import JsonValue

from plan_contracts import PlanError, StageSpec
from plan_records import clean_files
import goal_file_scope

PROTECTED: Final = frozenset({'dispatched', 'running', 'executor_completed', 'accepted',
                             'final_accepted', 'recovery_pending'})
FIELDS: Final = ('title', 'objective', 'kind', 'files', 'acceptance', 'candidates',
                 'fixed_model', 'depends_on', 'session_policy')


class NormalizedPlan(TypedDict):
    stages: list[dict[str, JsonValue]]
    added: list[str]
    updated: list[str]
    superseded: list[str]
    dependency_changes: list[dict[str, JsonValue]]


def decode_stages(rows: list) -> list[dict[str, JsonValue]]:
    """Read stage snapshots without changing task receipts or their ownership."""
    result: list[dict[str, JsonValue]] = []
    for raw in rows:
        row = dict(raw)
        for field in ('files', 'acceptance', 'candidates', 'depends_on', 'task_ids', 'data'):
            row[field] = json.loads(row[field])
        row['key'] = row['data'].get('dynamic_plan', {}).get('key', row['id'])
        result.append(row)
    return result


def is_superseded(stage: dict[str, JsonValue]) -> bool:
    """Historical removed stages stay queryable without acting as unfinished work."""
    return bool(stage['data'].get('dynamic_plan', {}).get('superseded_by'))


def requirements_superseded(stage: dict[str, JsonValue]) -> bool:
    """True only for a failed stage whose latest exact task was fenced as stale."""
    marker = stage['data'].get('requirements_superseded')
    task_ids = stage['task_ids']
    return bool(
        stage['status'] == 'failed'
        and isinstance(marker, dict)
        and task_ids
        and marker.get('task_id') == task_ids[-1]
    )


def normalize(specs: list[StageSpec], existing: list[dict[str, JsonValue]],
              allowed_files: list[str], revision: int, *, project: str,
              file_scope: Literal['project', 'selected']) -> NormalizedPlan:
    """Resolve stable keys and reject attempts to rewrite work already executed."""
    current = {row['id']: row for row in existing}
    aliases = {row['key']: row['id'] for row in existing}
    keys: set[str] = set()
    stages: list[dict[str, JsonValue]] = []
    added: list[str] = []
    updated: list[str] = []
    changes: list[dict[str, JsonValue]] = []
    selected: set[str] = set()
    for spec in specs:
        if spec.key in keys:
            raise PlanError('duplicate_key', '阶段 key 不得重复：' + spec.key)
        keys.add(spec.key)
        stage_id = spec.id or aliases.get(spec.key) or 'stage_' + uuid4().hex
        if stage_id in selected:
            raise PlanError('duplicate_stage', '同一阶段不能在计划中出现两次')
        if spec.id and spec.id not in current:
            raise PlanError('foreign_stage', '阶段 ID 不属于当前目标：' + spec.id)
        selected.add(stage_id)
        aliases[spec.key] = stage_id
        row = spec.model_dump()
        row['id'] = stage_id
        row['files'] = goal_file_scope.validate_files(project, file_scope, allowed_files, clean_files(spec.files))
        if spec.kind == 'edit' and not row['files']:
            raise PlanError('edit_scope', '修改阶段必须列出本阶段具体文件，项目范围无需人类预先枚举')
        stages.append(row)
    for row in stages:
        row['depends_on'] = list(dict.fromkeys(aliases.get(ref, ref) for ref in row['depends_on']))
        prior = current.get(row['id'])
        if prior is None:
            added.append(row['id'])
            continue
        if is_superseded(prior):
            raise PlanError('retired_stage', '被替换的阶段不能复活；请使用新 key 建立后继阶段')
        changed = [name for name in FIELDS if row[name] != prior[name]]
        if changed and (prior['status'] in PROTECTED or prior['task_ids']):
            raise PlanError('execution_immutable', '已执行阶段不能改写；请新增后继阶段并保留历史')
        if prior['key'] != row['key']:
            raise PlanError('key_immutable', '既有阶段的 key 不能更名')
        if changed:
            updated.append(row['id'])
        if prior['depends_on'] != row['depends_on']:
            changes.append({'stage_id': row['id'], 'before': prior['depends_on'],
                            'after': row['depends_on']})
    superseded: list[str] = []
    for prior in existing:
        if prior['id'] in selected or is_superseded(prior):
            continue
        if requirements_superseded(prior):
            superseded.append(prior['id'])
        elif prior['status'] in PROTECTED or prior['task_ids']:
            stages.append({**prior, 'depends_on': prior['depends_on']})
        else:
            superseded.append(prior['id'])
    check_dag(stages)
    return {'stages': stages, 'added': added, 'updated': updated,
            'superseded': superseded, 'dependency_changes': changes}


def check_dag(stages: list[dict[str, JsonValue]]) -> None:
    """A dependency must be live in this goal and the entire graph must be acyclic."""
    graph = {row['id']: row['depends_on'] for row in stages}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(stage_id: str) -> None:
        if stage_id in visiting:
            raise PlanError('dependency_cycle', '计划依赖存在循环，未保存任何修改')
        if stage_id in visited:
            return
        if stage_id not in graph:
            raise PlanError('dependency_missing', '依赖指向不存在、其他目标或被替换的阶段：' + stage_id)
        visiting.add(stage_id)
        for dependency in graph[stage_id]:
            visit(dependency)
        visiting.remove(stage_id)
        visited.add(stage_id)

    for stage_id in graph:
        visit(stage_id)
