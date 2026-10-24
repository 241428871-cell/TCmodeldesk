"""Reusable API accounts, independent of models; secrets remain in the OS vault."""
from __future__ import annotations

import os
from typing import Literal, TypedDict
from urllib.parse import urlparse
from uuid import uuid4

import model_registry as registry
from native_metadata import JsonMap
from pydantic import BaseModel, ConfigDict, Field

Provider = Literal['local', 'deepseek', 'glm', 'custom']


class AccountError(ValueError):
    """A safe account configuration error."""


class AccountInput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    id: str = ''
    name: str = Field(min_length=1, max_length=100)
    provider: Provider
    api_base: str
    api_key: str = ''
    api_key_env: str = Field(default='', pattern=r'^([A-Za-z_][A-Za-z0-9_]*)?$')
    clear_key: bool = False
    bypass_proxy: bool = False
    enabled: bool = True


class AccountRecord(BaseModel):
    model_config = ConfigDict(extra='ignore', frozen=True)
    id: str
    name: str
    provider: Provider
    api_base: str
    secret: str = ''
    api_key_env: str = ''
    bypass_proxy: bool = False
    enabled: bool = True
    created_at: str = ''


class PublicAccount(TypedDict):
    id: str
    name: str
    provider: str
    api_base: str
    api_key_set: bool
    api_key_env: str
    bypass_proxy: bool
    enabled: bool
    legacy: bool
    model_count: int


def _records(data: JsonMap) -> list[AccountRecord]:
    rows = [AccountRecord.model_validate(row) for row in data.get('accounts', [])]
    ids = {row.id for row in rows}
    for model in data.get('models', []):
        if model.get('provider') not in ('local', 'deepseek', 'glm', 'custom') or model.get('account_id'):
            continue
        ident = 'legacy-' + model['id']
        if ident not in ids:
            rows.append(AccountRecord.model_validate({**model, 'id': ident,
                'name': model['name'] + ' · 原有账户', 'enabled': True}))
    return rows


def _public(row: AccountRecord, data: JsonMap) -> PublicAccount:
    return {'id': row.id, 'name': row.name, 'provider': row.provider, 'api_base': row.api_base,
            'api_key_set': bool(row.secret or os.environ.get(row.api_key_env, '')),
            'api_key_env': row.api_key_env, 'bypass_proxy': row.bypass_proxy, 'enabled': row.enabled,
            'legacy': row.id.startswith('legacy-') and not any(a['id'] == row.id for a in data.get('accounts', [])),
            'model_count': sum(model.get('account_id', 'legacy-' + model['id']) == row.id
                               for model in data.get('models', []))}


def listing() -> list[PublicAccount]:
    """Expose safe account metadata, including read-only legacy references."""
    with registry.registry_lock():
        data = registry.read_registry()
    return [_public(row, data) for row in _records(data)]


def get(ident: str) -> AccountRecord:
    with registry.registry_lock():
        data = registry.read_registry()
    row = next((row for row in _records(data) if row.id == ident), None)
    if row is None:
        raise AccountError('账户不存在，请重新选择账户')
    return row


def connection(ident: str, *, secret: bool = False) -> JsonMap:
    """Resolve credentials only at an explicit connection/task boundary."""
    row = get(ident)
    if secret and not row.enabled:
        raise AccountError('此账户已停用，请在账户管理中启用')
    result: JsonMap = {'provider': row.provider, 'api_base': row.api_base,
                      'api_key_env': row.api_key_env, 'bypass_proxy': row.bypass_proxy,
                      'secret': row.secret, 'account_id': row.id, 'account_name': row.name,
                      'account_enabled': row.enabled}
    if secret:
        result['api_key'] = registry.protect(row.secret, True) if row.secret else os.environ.get(row.api_key_env, '')
    return result


def upsert(body: JsonMap) -> PublicAccount:
    """Save an account without requiring or creating any model."""
    request = AccountInput.model_validate(body)
    base = request.api_base.strip().rstrip('/')
    url = urlparse(base)
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
            or url.query or url.fragment or not request.name.strip()):
        raise AccountError('请填写账户名称与有效的 http(s) Base URL，地址不能包含凭据或查询参数')
    with registry.registry_lock():
        data = registry.read_registry()
        previous = next((row for row in _records(data) if row.id == request.id), None)
        if request.id and previous is None:
            raise AccountError('账户已不存在，请重新打开账户管理')
        if previous and request.provider != previous.provider:
            raise AccountError('已有账户不能更换服务商；请另行添加账户')
        reference = previous.secret if previous else ''
        if request.clear_key:
            reference = ''
        elif request.api_key:
            reference = registry.protect(request.api_key)
        row = AccountRecord(id=request.id or 'acct_' + uuid4().hex[:12], name=request.name.strip(),
            provider=request.provider, api_base=base, secret=reference,
            api_key_env=request.api_key_env, bypass_proxy=request.bypass_proxy, enabled=request.enabled,
            created_at=previous.created_at if previous else registry.now())
        saved = [account for account in data.get('accounts', []) if account['id'] != row.id]
        data['accounts'] = [*saved, row.model_dump()]
        # One atomic registry write moves legacy credentials without copying keys.
        for model in data['models']:
            if model.get('account_id', 'legacy-' + model['id']) == row.id:
                model['account_id'] = row.id
                model['secret'] = ''
                model['api_key_env'] = ''
        registry.write_json(registry.REGISTRY, data)
    return _public(row, data)


def delete(ident: str) -> None:
    """Refuse removal while models still reference the account."""
    with registry.registry_lock():
        data = registry.read_registry()
        if any(model.get('account_id', 'legacy-' + model['id']) == ident for model in data['models']):
            raise AccountError('仍有模型使用此账户，请先更换或移除这些模型')
        rows = data.get('accounts', [])
        if not any(row['id'] == ident for row in rows):
            raise AccountError('账户不存在')
        data['accounts'] = [row for row in rows if row['id'] != ident]
        registry.write_json(registry.REGISTRY, data)


def adopt(data: JsonMap, ident: str) -> None:
    """Promote a legacy reference within the caller's atomic model-save write."""
    if any(row['id'] == ident for row in data.get('accounts', [])):
        return
    row = next((row for row in _records(data) if row.id == ident), None)
    if row is None:
        raise AccountError('账户已变更，请刷新后保存模型')
    data['accounts'] = [*data.get('accounts', []), row.model_dump()]
