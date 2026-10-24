"""Read native CLI metadata without sending prompts or starting model work.

Account and catalog refreshes are explicit. Snapshot reads only inspect memory;
no tokens, email addresses, raw CLI errors or credential files cross this API.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from threading import Lock
from typing import Final, NotRequired, TypedDict

import deepseek_billing
import model_registry as registry
from native_metadata import JsonMap, JsonValue, NativeMetadata as _NativeMetadata, ProviderError


class ModelInfo(TypedDict):
    id: str
    name: str
    description: str
    effort_levels: list[str]
    default_effort: str
    is_default: bool
    fast_supported: bool
    context_window: NotRequired[int]


class Catalog(TypedDict, total=False):
    provider: str
    models: list[ModelInfo]
    state: str
    source: str
    live: bool
    updated_at: str
    message: str
    error: str
    stale: bool
    capabilities: JsonMap


class Account(TypedDict, total=False):
    provider: str
    state: str
    source: str
    updated_at: str
    message: str
    error: str
    windows: list[JsonMap]
    balance: list[JsonMap]
    balance_infos: list[JsonMap]
    subscription_type: str
    stale: bool
    available: bool


_LOCK: Final = Lock()
_CATALOGS: Final[dict[str, Catalog]] = {}
_ACCOUNTS: Final[dict[str, Account]] = {}
_GENERATIONS: Final[dict[str, int]] = {}
_PROVIDERS: Final = {'claude_cli': 'claude', 'codex_cli': 'codex'}
_EFFORTS: Final = {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'}


def invalidate(provider: str) -> None:
    """Discard metadata from a previous backend, including in-flight refreshes."""
    with _LOCK:
        _GENERATIONS[provider] = _GENERATIONS.get(provider, 0) + 1
        _CATALOGS.pop(provider, None)
        _ACCOUNTS.pop(provider, None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _map(value: JsonValue) -> JsonMap:
    return value if isinstance(value, dict) else {}


def _text(value: JsonValue, limit: int = 500) -> str:
    return value[:limit] if isinstance(value, str) else ''


def _number(value: JsonValue) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None



def _efforts(raw: JsonValue) -> list[str]:
    rows = raw if isinstance(raw, list) else []
    values = [_text(item) if isinstance(item, str) else _text(_map(item).get('reasoningEffort')) for item in rows]
    return list(dict.fromkeys(value for value in values if value in _EFFORTS))


def _codex_models() -> list[ModelInfo]:
    models: list[ModelInfo] = []
    with _NativeMetadata('codex_cli') as session:
        cursor = ''
        for _ in range(5):
            params: JsonMap = {'limit': 100, 'includeHidden': False}
            if cursor:
                params['cursor'] = cursor
            reply = session.call('model/list', params)
            rows = reply.get('data')
            for item in rows if isinstance(rows, list) else []:
                row = _map(item)
                model_id = _text(row.get('model') or row.get('id'), 200)
                if not model_id:
                    continue
                tiers = row.get('serviceTiers') or row.get('additionalSpeedTiers') or []
                supported_tiers = {_text(tier) if isinstance(tier, str)
                                   else _text(_map(tier).get('id') or _map(tier).get('serviceTier'))
                                   for tier in tiers if isinstance(tiers, list)}
                models.append({'id': model_id, 'name': _text(row.get('displayName')) or model_id,
                    'description': _text(row.get('description')),
                    'effort_levels': _efforts(row.get('supportedReasoningEfforts')),
                    'default_effort': _text(row.get('defaultReasoningEffort')),
                    'is_default': row.get('isDefault') is True,
                    'fast_supported': bool(supported_tiers & {'fast', 'priority'})})
            cursor = _text(reply.get('nextCursor'))
            if not cursor:
                break
    return models


def _claude_models() -> list[ModelInfo]:
    with _NativeMetadata('claude_cli') as session:
        reply = session.call('list_models', {})
        rows = reply.get('models') or session.initial.get('models')
        models: list[ModelInfo] = []
        for item in rows if isinstance(rows, list) else []:
            row = _map(item)
            model_id = _text(row.get('value') or row.get('id'), 200)
            if model_id:
                models.append({'id': model_id, 'name': _text(row.get('displayName')) or model_id,
                    'description': _text(row.get('description')),
                    'effort_levels': _efforts(row.get('supportedEffortLevels')),
                    'default_effort': _text(row.get('defaultEffort')),
                    'is_default': model_id == 'default', 'fast_supported': False})
        return models


def _capabilities(provider: str) -> JsonMap:
    """Claude options follow the installed version's help; Codex options are config keys."""
    import cli_worker
    flags = cli_worker.installed_flags(provider) if provider == 'claude_cli' else None
    return {'effort': flags is None or '--effort' in flags, 'fast': provider == 'codex_cli',
            'autocompact': flags is None or '--autocompact' in flags,
            'autocompact_min': 100000 if provider == 'claude_cli' else 1024,
            'autocompact_max': 1000000 if provider == 'claude_cli' else 2000000,
            'version_checked': flags is not None}


def catalog_snapshot(provider: str) -> Catalog:
    """Return the last catalog without launching a CLI or contacting a provider."""
    with _LOCK:
        return deepcopy(_CATALOGS.get(provider, {'provider': provider, 'models': [],
            'state': 'unknown', 'source': 'native-cli', 'live': False,
            'message': '点击读取列表，从已登录的本机 CLI 获取模型与可用参数'}))


def catalog(provider: str) -> Catalog:
    """Explicitly discover native models; no generation, credentials or sessions."""
    if provider not in _PROVIDERS:
        raise ProviderError('此服务类型请通过其 API 模型列表读取')
    with _LOCK:
        generation = _GENERATIONS.get(provider, 0)
    source = 'claude-sdk-control/list_models' if provider == 'claude_cli' else 'codex-app-server/model/list'
    try:
        models = _claude_models() if provider == 'claude_cli' else _codex_models()
        if not models:
            raise ProviderError('本机 CLI 没有返回模型，请检查登录和组织模型权限')
        result: Catalog = {'provider': provider, 'models': models, 'state': 'ready',
            'source': source, 'live': True, 'updated_at': _now(),
            'capabilities': _capabilities(provider),
            'message': '本机 CLI 返回的模型目录；账户限制和具体任务可用性仍由服务商决定'}
    except ProviderError as error:
        result = {**catalog_snapshot(provider), 'provider': provider, 'state': 'error',
                  'live': False, 'error': str(error), 'message': str(error),
                  'stale': bool(catalog_snapshot(provider).get('models'))}
    with _LOCK:
        if generation == _GENERATIONS.get(provider, 0):
            _CATALOGS[provider] = result
        else:
            return {'provider': provider, 'models': [], 'state': 'unknown', 'live': False,
                    'message': '执行端来源已变更，请重新读取模型列表'}
    return deepcopy(result)


def _window(label: str, row: JsonMap, provider: str) -> JsonMap | None:
    used = _number(row.get('utilization') if provider == 'claude_cli' else row.get('usedPercent'))
    if used is None:
        return None
    used = max(0.0, min(100.0, used))
    reset = row.get('resets_at') if provider == 'claude_cli' else row.get('resetsAt')
    if isinstance(reset, (int, float)) and not isinstance(reset, bool):
        try:
            reset = datetime.fromtimestamp(reset, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            reset = None
    return {'label': label, 'used_percent': used, 'remaining_percent': 100.0 - used,
            'resets_at': _text(reset), 'window_minutes': row.get('windowDurationMins')}


def _claude_account() -> Account:
    with _NativeMetadata('claude_cli') as session:
        reply = session.call('get_usage', {'skip_behaviors': True})
    limits = _map(reply.get('rate_limits'))
    windows: list[JsonMap] = []
    for key, label in (('five_hour', '5 小时额度'), ('seven_day', '7 天额度'),
                       ('seven_day_opus', 'Opus · 7 天'), ('seven_day_sonnet', 'Sonnet · 7 天')):
        window = _window(label, _map(limits.get(key)), 'claude_cli')
        if window is not None:
            windows.append(window)
    scoped = limits.get('model_scoped')
    for item in scoped if isinstance(scoped, list) else []:
        row = _map(item)
        window = _window(_text(row.get('display_name')) or '模型额度', row, 'claude_cli')
        if window is not None:
            windows.append(window)
    available = reply.get('rate_limits_available') is True and bool(windows)
    return {'provider': 'claude_cli', 'state': 'ready' if available else 'unavailable',
        'source': 'claude-sdk-control/get_usage', 'updated_at': _now(), 'windows': windows,
        'subscription_type': _text(reply.get('subscription_type')), 'available': available,
        'message': '来自当前 Claude CLI 登录账户' if available else
            '当前 Claude CLI 未返回订阅额度；API Key、第三方网关或缺少账户权限时可能不可读取'}


def _codex_account() -> Account:
    with _NativeMetadata('codex_cli') as session:
        reply = session.call('account/rateLimits/read', {})
    groups = _map(reply.get('rateLimitsByLimitId'))
    if not groups:
        groups = {'default': reply.get('rateLimits')}
    windows: list[JsonMap] = []
    for group_name, raw in groups.items():
        group = _map(raw)
        prefix = _text(group.get('limitName')) or (group_name if group_name != 'default' else 'Codex')
        for key, fallback in (('primary', '主要窗口'), ('secondary', '次要窗口')):
            row = _map(group.get(key))
            minutes = _number(row.get('windowDurationMins'))
            label = f'{int(minutes / 60)} 小时' if minutes and minutes % 60 == 0 else fallback
            window = _window(prefix + ' · ' + label, row, 'codex_cli')
            if window is not None:
                windows.append(window)
    return {'provider': 'codex_cli', 'state': 'ready' if windows else 'unavailable',
        'source': 'codex-app-server/account/rateLimits/read', 'updated_at': _now(),
        'windows': windows, 'available': bool(windows),
        'message': '来自当前 Codex 登录账户' if windows else 'Codex 未返回可读取的账户额度'}


def _deepseek_account(model_id: str, force: bool) -> Account:
    models = registry.list_models().get('models', [])
    row = next((item for item in models if (not model_id or item['id'] == model_id)
                and deepseek_billing.is_official(item)), None)
    base: Account = {'provider': 'deepseek', 'source': deepseek_billing.BALANCE_DOC_URL,
                     'state': 'unknown', 'message': '添加 DeepSeek 官方 API 模型和密钥后查看余额'}
    if row is None:
        return base
    if force:
        raw = deepseek_billing.balance_state(row, force=True)
    else:
        identity = deepseek_billing.cache_identity(row)
        with deepseek_billing.LOCK:
            saved = deepseek_billing.CACHE.get(row['id'], {})
            raw = deepcopy(saved) if saved.get('identity') == identity[1] else {}
    balances = raw.get('balance_infos') or []
    return {**base, 'state': str(raw.get('status') or 'unknown'),
            'balance': balances, 'balance_infos': balances,
            'available': raw.get('is_available') is True,
            'updated_at': str(raw.get('updated_at') or ''), 'stale': bool(raw.get('stale')),
            'message': str(raw.get('error') or ('DeepSeek 官方 API 余额' if balances else '点击刷新读取官方账户余额'))}


ACCOUNT_TTL_SECONDS: Final = 15 * 60


def _instant(value: JsonValue) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _fresh(account: Account, now: datetime | None = None) -> Account:
    """A cached window past its reset no longer describes the account; old reads are stale."""
    moment = now or datetime.now(timezone.utc)
    windows: list[JsonMap] = []
    expired = 0
    for window in account.get('windows', []):
        reset = _instant(window.get('resets_at'))
        if reset is not None and reset <= moment:
            expired += 1
            windows.append({**window, 'used_percent': None, 'remaining_percent': None, 'expired': True})
        else:
            windows.append(window)
    result: Account = {**account, 'windows': windows}
    read_at = _instant(account.get('updated_at'))
    if expired or (read_at is not None and (moment - read_at).total_seconds() > ACCOUNT_TTL_SECONDS):
        result['stale'] = True
    if expired:
        result['message'] = '部分额度窗口已过重置时间，旧数值不再显示；请刷新读取当前额度'
    return result


def account_snapshot(provider: str, model_id: str = '', force: bool = False) -> Account:
    """Read cache, or explicitly refresh a native quota/official API balance."""
    if provider == 'deepseek':
        return _deepseek_account(model_id, force)
    if provider not in _PROVIDERS:
        raise ProviderError('此服务尚未提供账户额度读取接口')
    with _LOCK:
        generation = _GENERATIONS.get(provider, 0)
        cached = deepcopy(_ACCOUNTS.get(provider, {'provider': provider, 'state': 'unknown',
            'source': 'native-cli', 'windows': [], 'available': False,
            'message': '点击刷新读取当前 CLI 登录账户的额度'}))
    if not force:
        return _fresh(cached)
    try:
        result = _claude_account() if provider == 'claude_cli' else _codex_account()
    except ProviderError as error:
        result = {**cached, 'state': 'error', 'message': str(error), 'error': str(error),
                  'stale': bool(cached.get('updated_at'))}
    with _LOCK:
        if generation == _GENERATIONS.get(provider, 0):
            _ACCOUNTS[provider] = result
        else:
            return {'provider': provider, 'state': 'unknown', 'available': False, 'windows': [],
                    'message': '执行端来源已变更，请重新读取额度'}
    return _fresh(deepcopy(result))
