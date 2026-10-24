"""Behavioral checks for accounts, native DSH and collaboration preferences."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import connection_accounts as accounts
import account_metadata
import collaboration_policy
import dsh_runtime
import dsh_worker
import engineering_policy
import execution_engines
import model_registry as registry
import provider_accounts
import workflow_settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, 'HERE', tmp_path)
    monkeypatch.setattr(registry, 'REGISTRY', tmp_path / 'models.json')
    monkeypatch.setattr(registry, 'RUNS', tmp_path / 'runs')
    vault = {}
    def protect(value, decrypt=False):
        if decrypt:
            return vault[value]
        reference = 'fixture:' + str(len(vault))
        vault[reference] = value
        return reference
    monkeypatch.setattr(registry, 'protect', protect)
    provider_accounts.invalidate('dsh')
    yield
    provider_accounts.invalidate('dsh')


def add_account():
    return accounts.upsert({'name': 'Official API', 'provider': 'deepseek',
        'api_base': 'https://api.deepseek.com', 'api_key': 'private-fixture-key'})


def test_account_can_exist_without_any_model_and_keys_never_cross_public_boundary():
    account = add_account()
    assert registry.list_models()['models'] == []
    assert account['model_count'] == 0
    assert account['api_key_set'] is True
    assert 'private-fixture-key' not in registry.REGISTRY.read_text()
    assert 'secret' not in json.dumps(accounts.listing())
    assert workflow_settings.snapshot()['paid_allowed'] is False


def test_multiple_models_share_one_account_and_rotation_updates_resolution():
    account = add_account()
    one = registry.upsert_model({'name': 'One', 'model': 'deepseek-flash', 'account_id': account['id']})
    two = registry.upsert_model({'name': 'Two', 'model': 'deepseek-v4-pro', 'account_id': account['id']})
    assert registry.get_model(one['id'])['api_key'] == 'private-fixture-key'
    accounts.upsert({'id': account['id'], 'name': account['name'], 'provider': 'deepseek',
        'api_base': 'https://api.deepseek.com', 'api_key': 'rotated-key'})
    assert registry.get_model(two['id'])['api_key'] == 'rotated-key'
    assert accounts.listing()[0]['model_count'] == 2
    with pytest.raises(accounts.AccountError, match='仍有模型'):
        accounts.delete(account['id'])
    assert not any('rotated-key' in json.dumps(row) for row in registry.list_models()['models'])


def test_legacy_promotion_preserves_the_same_vault_reference():
    legacy = registry.upsert_model({'name': 'Old', 'model': 'deepseek-flash', 'provider': 'deepseek',
        'api_base': 'https://api.deepseek.com', 'api_key': 'legacy-key'})
    before = registry.REGISTRY.read_bytes()
    old = accounts.listing()[0]
    assert old['legacy'] and registry.REGISTRY.read_bytes() == before
    saved = registry.upsert_model({'name': 'Old', 'model': 'deepseek-flash', 'account_id': old['id']}, legacy['id'])
    assert saved['account_id'] == old['id']
    assert registry.get_model(legacy['id'])['api_key'] == 'legacy-key'
    assert not accounts.listing()[0]['legacy']


def test_balance_without_a_model_is_explicit_and_account_key_changes_drop_cache(monkeypatch):
    account = add_account()
    reads = []
    def read(row):
        reads.append(row['api_key'])
        return {'is_available': True, 'balance_infos': [{'currency': 'CNY', 'total_balance': '7'}]}
    monkeypatch.setattr(account_metadata.deepseek_billing, 'read_balance', read)
    assert account_metadata.balance(account['id'])['state'] == 'unknown'
    assert reads == []
    assert account_metadata.balance(account['id'], force=True)['state'] == 'ready'
    accounts.upsert({'id': account['id'], 'name': account['name'], 'provider': 'deepseek',
        'api_base': 'https://api.deepseek.com', 'api_key': 'new-key'})
    assert account_metadata.balance(account['id'])['state'] == 'unknown'


def test_dsh_catalog_preserves_provider_route_and_native_model_import(monkeypatch):
    import dsh_metadata
    monkeypatch.setattr(dsh_metadata, 'probe', lambda **kwargs: {'models': [
        {'provider': 'deepseek-account', 'id': 'flash', 'name': 'Flash',
         'context_window': 1048576, 'effort_levels': ['off', 'low', 'high', 'max']} ]})
    result = provider_accounts.catalog('dsh')
    assert result['state'] == 'ready'
    entry = result['models'][0]
    assert json.loads(entry['id']) == ['deepseek-account', 'flash']
    assert entry['effort_levels'] == ['off', 'low', 'high', 'max']
    model = registry.upsert_model({'provider': 'dsh', 'name': entry['name'], 'model': entry['id'], 'default_agent': 'dsh'})
    assert registry.get_model(model['id'])['api_key'] == ''
    with pytest.raises(ValueError, match='只能由 DSH'):
        execution_engines.validate_model('direct', model)


def test_dsh_runtime_reaches_real_compaction_and_native_auth_patch():
    model = {'provider': 'dsh', 'name': 'DSH', 'model': '["deepseek-account","flash"]',
        'api_base': '', 'max_input_tokens': 1048576, 'max_output_tokens': 4096}
    effective = dsh_runtime.apply(model, {'thinking_level': 'high', 'context_window': 200000,
        'autocompact_tokens': 150000})
    rows = dsh_worker.build_patch(effective, 'chat')
    account = next(row for row in rows if row['id'] == 'llm-deepseek-account')
    assert account['disabled'] is False and account['config']['reasoningEffort'] == 'high'
    assert next(row for row in rows if row['id'] == 'acp')['config']['provider'] == 'deepseek-account'
    compact = next(row for row in rows if row['id'] == 'compaction-basic')['config']['modelPolicies'][0]
    assert compact['thresholdRatio'] == .75
    assert next(row for row in rows if row['id'] == 'skill-filesystem')['config']['dshHome']
    assert not model.get('_dsh_runtime')


def test_native_skill_mode_does_not_embed_the_engineering_catalog():
    policy = engineering_policy.task_context('')
    assert 'tcauto' in policy['prompt']
    assert 'available_rules' not in policy['snapshot']
    assert all('text' not in row for row in policy['snapshot']['effective_rules'])
    collaboration_policy.save({'skill_mode': 'off'})
    assert engineering_policy.task_context('')['prompt'] == ''


def test_new_install_collaborates_by_default_but_existing_stop_and_fees_are_preserved():
    assert workflow_settings.snapshot()['enabled'] is True
    workflow_settings.save_settings({'enabled': False})
    assert workflow_settings.snapshot()['enabled'] is False
    assert workflow_settings.snapshot()['paid_allowed'] is False


def test_disabled_shared_account_is_not_an_enabled_connection():
    account = add_account()
    model = registry.upsert_model({'name': 'Linked', 'model': 'deepseek-flash', 'account_id': account['id']})
    accounts.upsert({'id': account['id'], 'name': account['name'], 'provider': 'deepseek',
        'api_base': 'https://api.deepseek.com', 'enabled': False})
    assert registry.list_models()['models'][0]['account_enabled'] is False
    with pytest.raises(accounts.AccountError, match='已停用'):
        registry.get_model(model['id'])


def test_explicit_dsh_refresh_clears_unavailable_wallets_and_does_not_fallback(monkeypatch):
    import dsh_metadata
    import host_balance
    monkeypatch.setattr(dsh_metadata, 'probe', lambda **kwargs: {'models': [], 'account': {
        'status': 'ready', 'wallets': [{'kind': 'normal', 'currency': 'CNY', 'balance': '7'}]}})
    assert provider_accounts.account_snapshot('dsh', force=True)['available'] is True
    def unavailable(**kwargs):
        raise provider_accounts.ProviderError('metadata unavailable')
    monkeypatch.setattr(dsh_metadata, 'probe', unavailable)
    failed = provider_accounts.account_snapshot('dsh', force=True)
    assert failed['state'] == 'error' and failed['balance'] == []
    assert host_balance.state()['wallets'] == []
