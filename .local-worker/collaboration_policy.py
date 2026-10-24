"""Installation preferences for delegation hints, concurrency and skill delivery."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import model_registry as registry
from native_metadata import JsonMap
from pydantic import BaseModel, ConfigDict, Field


class CollaborationSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    schema: Literal[1] = 1
    guidance: str = Field(default='独立任务按需并行；执行完成由主脑读取实际成果并独立验收。不要为了多 agent 而拆碎任务。', max_length=12000)
    codex_hint: str = Field(default='优先考虑逻辑分析、疑难排查、后端设计与独立验收；按任务实际表现调整。', max_length=6000)
    claude_hint: str = Field(default='优先考虑前端、交互与视觉实现；按任务实际表现调整。', max_length=6000)
    dsh_hint: str = Field(default='优先考虑边界明确、可快速执行的小任务；按任务实际表现调整。', max_length=6000)
    account_concurrency: int = Field(default=8, ge=1, le=64)
    skill_mode: Literal['native', 'inline', 'off'] = 'native'


def snapshot() -> JsonMap:
    """Read preferences only; do not install rules, run models or start timers."""
    path = Path(registry.HERE) / 'collaboration-policy.json'
    settings = CollaborationSettings.model_validate_json(path.read_bytes()) if path.exists() else CollaborationSettings()
    return settings.model_dump()


def save(body: JsonMap) -> JsonMap:
    with registry.file_lock(Path(registry.HERE) / 'collaboration-policy.lock'):
        settings = CollaborationSettings.model_validate({**snapshot(), **body})
        registry.write_json(Path(registry.HERE) / 'collaboration-policy.json', settings.model_dump())
    return settings.model_dump()


def context(project: str) -> JsonMap:
    """The planner receives human hints and per-model project assignments."""
    import workspace_config
    owner = workspace_config.project_config(project) if project else None
    return {**snapshot(), 'bindings': [
        {'model_id': binding['model_id'], 'role': binding['role'],
         'hint': binding.get('collaboration_hint', ''), 'max_instances': binding.get('max_instances', 8)}
        for binding in (owner or {}).get('bindings', []) if binding['enabled']]}


def task_hint(project: str, model_id: str) -> str:
    """Keep the executor's assignment prompt separate from model credentials."""
    policy = context(project)
    hints = [row['hint'] for row in policy['bindings'] if row['model_id'] == model_id and row['role'] == 'execution' and row['hint']]
    return '\n\n本项目为此模型指定的分工提示：\n' + '\n'.join(hints) if hints else ''
