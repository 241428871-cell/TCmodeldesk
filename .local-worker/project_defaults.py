from __future__ import annotations

from pathlib import Path
from typing import Final, Literal, Self
from uuid import uuid4

import model_registry as registry
import workspace_config
from native_metadata import JsonMap
from pydantic import BaseModel, ConfigDict, Field, model_validator

NAME: Final = 'project-defaults.json'


class DefaultsError(ValueError):
    pass


class ModelDefaults(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    model_id: str
    provider: str
    model: str
    role: Literal['decision', 'execution']
    enabled: bool
    is_default: bool
    effort: Literal['default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']
    fast: bool
    context_window: int = Field(ge=1024, le=2_000_000)
    autocompact_tokens: int | None = Field(ge=1024, le=2_000_000)
    thinking_level: Literal['default', 'off', 'low', 'medium', 'high', 'xhigh', 'max'] | None = None
    max_instances: int | None = Field(default=None, ge=1, le=64)
    collaboration_hint: str = Field(default='', max_length=12000)

    @model_validator(mode='after')
    def bounded_runtime(self) -> Self:
        if self.autocompact_tokens is not None and self.autocompact_tokens >= self.context_window:
            raise DefaultsError('默认模板的压缩阈值必须小于上下文预算')
        if self.fast and self.provider != 'codex_cli':
            raise DefaultsError('默认模板的 fast 只能用于 Codex CLI')
        return self

    def binding(self) -> workspace_config.Binding:
        result: workspace_config.Binding = {
            'id': uuid4().hex[:12], 'model_id': self.model_id, 'role': self.role,
            'enabled': self.enabled, 'is_default': self.is_default,
            'effort': self.effort, 'fast': self.fast, 'context_window': self.context_window,
            'autocompact_tokens': self.autocompact_tokens}
        if 'collaboration_hint' in self.model_fields_set:
            result['collaboration_hint'] = self.collaboration_hint
        if self.thinking_level is not None:
            result['thinking_level'] = self.thinking_level
        if self.max_instances is not None:
            result['max_instances'] = self.max_instances
        return result


class ProjectDefaults(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    schema_version: Literal[1] = Field(default=1, alias='schema')
    updated_at: str
    work_concurrency: int = Field(ge=1, le=64)
    bindings: list[ModelDefaults]

    @model_validator(mode='after')
    def unique_roles(self) -> Self:
        keys = [(row.model_id, row.role) for row in self.bindings]
        if len(keys) != len(set(keys)):
            raise DefaultsError('默认模板中同一角色的模型重复')
        for role in ('decision', 'execution'):
            if sum(row.role == role and row.is_default for row in self.bindings) > 1:
                raise DefaultsError('默认模板中每个角色只能有一个默认模型')
        return self


def _load() -> ProjectDefaults | None:
    path = Path(registry.HERE) / NAME
    return ProjectDefaults.model_validate_json(path.read_bytes()) if path.exists() else None


def status() -> JsonMap:
    try:
        saved = _load()
    except (OSError, ValueError):
        return {'configured': True, 'readable': False, 'error': '新项目默认模板不可读，请重新保存'}
    return {'configured': saved is not None, 'readable': True,
            'work_concurrency': saved.work_concurrency if saved else 8,
            'model_count': len(saved.bindings) if saved else 0,
            'updated_at': saved.updated_at if saved else ''}


def capture(project: str) -> JsonMap:
    owner = workspace_config.project_config(project)
    if owner is None:
        raise DefaultsError('请先选择已添加的项目')
    models = {row['id']: row for row in registry.list_models()['models']}
    bindings = []
    for binding in owner['bindings']:
        model = models.get(binding['model_id'])
        if model is None:
            raise DefaultsError('项目中的模型已被移除，请先更新模型关联')
        values = {key: binding[key] for key in workspace_config.Binding.__annotations__ if key in binding and key != 'id'}
        bindings.append(ModelDefaults.model_validate({**values, 'provider': model['provider'], 'model': model['model']}))
    saved = ProjectDefaults.model_validate({'schema': 1, 'updated_at': registry.now(),
        'work_concurrency': owner['work_concurrency'], 'bindings': bindings})
    with registry.file_lock(Path(registry.HERE) / 'project-defaults.lock'):
        registry.write_json(Path(registry.HERE) / NAME, saved.model_dump(by_alias=True, exclude_unset=True))
    return status()


def new_project() -> tuple[int, list[workspace_config.Binding]]:
    saved = _load()
    if saved is None:
        return 8, []
    models = {row['id']: row for row in registry.list_models()['models']}
    for row in saved.bindings:
        model = models.get(row.model_id)
        if model is None or (model['provider'], model['model']) != (row.provider, row.model):
            raise DefaultsError('默认模板引用的模型已删除或更换，请在项目设置中重新保存默认模板')
    return saved.work_concurrency, [row.binding() for row in saved.bindings]
