"""Translate immutable project runtime snapshots to DSH's actual native policy."""
from __future__ import annotations

import json
import os
from pathlib import Path

from native_metadata import JsonMap, ProviderError


def native_target(model: JsonMap) -> tuple[str, str]:
    """Keep the DSH provider identity, including account versus API authentication."""
    if model.get('provider') != 'dsh':
        return 'desk-worker', str(model['model'])
    try:
        value = json.loads(str(model['model']))
    except ValueError:
        raise ProviderError('DSH 模型标识需要来自原生模型扫描') from None
    if (not isinstance(value, list) or len(value) != 2
            or value[0] not in ('deepseek-account', 'deepseek-official')
            or not isinstance(value[1], str) or not value[1]):
        raise ProviderError('此 DSH 原生路由尚未接入，请使用账户与 API 模型入口')
    return value[0], value[1]


def credential_patch() -> JsonMap:
    """The official provider reads its own store; the workbench never copies keys."""
    return {'id': 'credentials', 'config': {
        'dshHome': os.environ.get('DSH_HOME') or str(Path.home() / '.dsh'), 'watch': False}}


def apply(model: JsonMap, runtime: JsonMap | None) -> JsonMap:
    """Apply new-task overrides while leaving model/account defaults untouched."""
    selected = dict(runtime or {})
    result = {**model, '_dsh_runtime': selected}
    if 'thinking_level' in selected:
        result['thinking_level'] = selected['thinking_level']
    if type(selected.get('context_window')) is int:
        result['max_input_tokens'] = min(int(model.get('max_input_tokens', 32768)), selected['context_window'])
    return result


def compaction_patch(model: JsonMap) -> list[JsonMap]:
    """Use real compaction-basic thresholds, bounded by output headroom."""
    runtime = model.get('_dsh_runtime') or {}
    context = runtime.get('context_window')
    threshold = runtime.get('autocompact_tokens')
    if context is None or threshold is None:
        return []
    if (type(context) is not int or type(threshold) is not int
            or not 1024 <= threshold < context <= 2_000_000):
        raise ProviderError('DSH 自动压缩阈值必须小于上下文预算（1024–2000000 tokens）')
    provider, ident = native_target(model)
    if int(model.get('max_output_tokens', 4096)) >= int(model.get('max_input_tokens', context)):
        raise ProviderError('DSH 上下文预算必须大于模型的单次输出上限，请增大上下文预算')
    # DSH additionally caps this threshold by the provider's real context and
    # reserved output. The form describes this as a ceiling, not an exact count.
    return [{'id': 'compaction-basic', 'config': {'modelPolicies': [
        {'provider': provider, 'model': ident,
         'thresholdRatio': min(.99, threshold / int(model.get('max_input_tokens', context))), 'headroomTokens': 0,
         'retainRatio': min(.16, threshold / int(model.get('max_input_tokens', context)) / 2)}]}}]
