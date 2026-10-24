"""Offline native metadata, quota and CLI argument contracts; no model calls."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_worker
import provider_accounts as providers
import native_metadata as protocol


class CaptureInput(io.StringIO):
    saved = ''

    def close(self):
        self.saved = self.getvalue()
        super().close()


class Process:
    pid = 1234

    def __init__(self, lines):
        self.stdin = CaptureInput()
        self.stdout = io.StringIO('\n'.join(json.dumps(line) for line in lines) + '\n')

    def poll(self):
        return 0

    def wait(self, timeout):
        return 0


class ProviderTests(unittest.TestCase):
    def setUp(self):
        providers._CATALOGS.clear()
        providers._ACCOUNTS.clear()

    def test_snapshots_never_launch_cli_or_fetch_balance(self):
        with patch.object(providers, '_NativeMetadata', side_effect=AssertionError('no CLI')), \
             patch.object(providers.registry, 'list_models', return_value={'models': []}), \
             patch.object(providers.deepseek_billing, 'balance_state', side_effect=AssertionError('no network')):
            self.assertEqual(providers.catalog_snapshot('codex_cli')['state'], 'unknown')
            self.assertEqual(providers.account_snapshot('claude_cli')['state'], 'unknown')
            self.assertEqual(providers.account_snapshot('deepseek')['state'], 'unknown')

    def test_codex_discovery_uses_only_metadata_and_preserves_effort_order(self):
        child = Process([{'id': 1, 'result': {}}, {'id': 2, 'result': {'data': [
            {'id': 'native-id', 'model': 'native-model', 'displayName': 'Native model',
             'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'max'}],
             'defaultReasoningEffort': 'low', 'serviceTiers': ['fast'], 'isDefault': True}
        ], 'nextCursor': None}}])
        with patch.object(protocol.cli_paths, 'executable', return_value='/fake/codex'), \
             patch.object(protocol.subprocess, 'Popen', return_value=child):
            result = providers.catalog('codex_cli')
        self.assertTrue(result['live'])
        self.assertEqual(result['models'][0]['id'], 'native-model')
        self.assertEqual(result['models'][0]['effort_levels'], ['low', 'max'])
        self.assertTrue(result['models'][0]['fast_supported'])
        requests = [json.loads(line) for line in child.stdin.saved.splitlines()]
        self.assertEqual([row['method'] for row in requests], ['initialize', 'initialized', 'model/list'])
        self.assertNotIn('prompt', child.stdin.saved)

    def test_claude_native_catalog_does_not_send_a_user_message(self):
        child = Process([{'type': 'control_response', 'response': {'subtype': 'success',
            'request_id': str(index), 'response': {'models': [
                {'value': 'sonnet', 'displayName': 'Sonnet native', 'supportsEffort': True,
                 'supportedEffortLevels': ['low', 'medium', 'high', 'xhigh', 'max']}]}}}
            for index in (1, 2)])
        with patch.object(protocol.cli_paths, 'executable', return_value='/fake/claude'), \
             patch.object(protocol.subprocess, 'Popen', return_value=child) as invoke:
            result = providers.catalog('claude_cli')
        self.assertEqual(result['models'][0]['id'], 'sonnet')
        requests = [json.loads(line) for line in child.stdin.saved.splitlines()]
        self.assertEqual([row['type'] for row in requests], ['control_request', 'control_request'])
        self.assertEqual([row['request']['subtype'] for row in requests], ['initialize', 'list_models'])
        arguments = invoke.call_args.args[0]
        self.assertIn('--no-session-persistence', arguments)
        self.assertIn('{"disableAllHooks":true}', arguments)
        self.assertIn('{"mcpServers":{}}', arguments)

    def test_claude_quota_drops_identity_and_distinguishes_null_from_zero(self):
        replies = [
            {'type': 'control_response', 'response': {'subtype': 'success', 'request_id': '1', 'response': {}}},
            {'type': 'control_response', 'response': {'subtype': 'success', 'request_id': '2', 'response': {
                'subscription_type': 'max', 'email': 'private@example.test',
                'rate_limits_available': True, 'rate_limits': {
                    'five_hour': {'utilization': 0, 'resets_at': '2099-10-22T20:00:00Z'},
                    'seven_day': {'utilization': None, 'resets_at': None}}}}}]
        child = Process(replies)
        with patch.object(protocol.cli_paths, 'executable', return_value='/fake/claude'), \
             patch.object(protocol.subprocess, 'Popen', return_value=child):
            result = providers.account_snapshot('claude_cli', force=True)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['windows'][0]['remaining_percent'], 100)
        self.assertEqual(len(result['windows']), 1)
        self.assertNotIn('email', json.dumps(result))
        self.assertNotIn('private@example.test', json.dumps(result))

    def test_codex_quota_handles_multiple_windows_and_reset_time(self):
        child = Process([{'id': 1, 'result': {}}, {'id': 2, 'result': {
            'rateLimitsByLimitId': {'codex': {'primary': {'usedPercent': 25,
                'windowDurationMins': 300, 'resetsAt': 4100000000},
                'secondary': {'usedPercent': None}}}}}])
        with patch.object(protocol.cli_paths, 'executable', return_value='/fake/codex'), \
             patch.object(protocol.subprocess, 'Popen', return_value=child):
            result = providers.account_snapshot('codex_cli', force=True)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['windows'][0]['remaining_percent'], 75)
        self.assertTrue(result['windows'][0]['resets_at'].endswith('+00:00'))
        self.assertEqual(len(result['windows']), 1)

    def test_refresh_failure_retains_last_snapshot_and_hides_native_errors(self):
        with patch.object(providers, '_claude_account', return_value={
                'provider': 'claude_cli', 'state': 'ready', 'updated_at': 'before',
                'windows': [{'remaining_percent': 60}]}):
            providers.account_snapshot('claude_cli', force=True)
        with patch.object(providers, '_claude_account', side_effect=providers.ProviderError('CLI 暂时不可用')):
            result = providers.account_snapshot('claude_cli', force=True)
        self.assertTrue(result['stale'])
        self.assertEqual(result['windows'][0]['remaining_percent'], 60)
        result['windows'].clear()
        self.assertEqual(len(providers.account_snapshot('claude_cli')['windows']), 1)

    def test_deepseek_snapshot_uses_matching_cached_identity_without_refresh(self):
        row = {'id': 'deepseek', 'provider': 'deepseek', 'api_base': 'https://api.deepseek.com', 'api_key_set': True}
        saved = {'identity': 'current-key', 'status': 'ready', 'is_available': True,
                 'balance_infos': [{'currency': 'CNY', 'total_balance': '12.30'}]}
        with patch.object(providers.registry, 'list_models', return_value={'models': [row]}), \
             patch.object(providers.deepseek_billing, 'cache_identity', return_value=('deepseek', 'current-key')), \
             patch.dict(providers.deepseek_billing.CACHE, {'deepseek': saved}, clear=True), \
             patch.object(providers.deepseek_billing, 'balance_state', side_effect=AssertionError('no refresh')):
            result = providers.account_snapshot('deepseek', 'deepseek')
        self.assertEqual(result['balance'][0]['total_balance'], '12.30')


class AccountFreshnessTests(unittest.TestCase):
    def setUp(self):
        providers._ACCOUNTS.clear()

    def test_window_past_its_reset_is_not_shown_as_current_quota(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        providers._ACCOUNTS['claude_cli'] = {
            'provider': 'claude_cli', 'state': 'ready', 'updated_at': now.isoformat(),
            'windows': [
                {'label': '5 小时额度', 'used_percent': 90.0, 'remaining_percent': 10.0,
                 'resets_at': (now - timedelta(minutes=1)).isoformat()},
                {'label': '7 天额度', 'used_percent': 20.0, 'remaining_percent': 80.0,
                 'resets_at': (now + timedelta(days=2)).isoformat()}]}
        result = providers.account_snapshot('claude_cli')
        self.assertTrue(result['stale'])
        self.assertIsNone(result['windows'][0]['remaining_percent'])
        self.assertTrue(result['windows'][0]['expired'])
        self.assertEqual(result['windows'][1]['remaining_percent'], 80.0)
        self.assertEqual(providers._ACCOUNTS['claude_cli']['windows'][0]['remaining_percent'], 10.0)

    def test_old_read_is_marked_stale_without_refreshing(self):
        from datetime import datetime, timedelta, timezone
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        providers._ACCOUNTS['codex_cli'] = {'provider': 'codex_cli', 'state': 'ready',
                                            'updated_at': old.isoformat(), 'windows': []}
        with patch.object(providers, '_NativeMetadata', side_effect=AssertionError('no CLI')):
            self.assertTrue(providers.account_snapshot('codex_cli')['stale'])


class RuntimeOptionsTests(unittest.TestCase):
    def test_codex_runtime_maps_project_settings_to_native_overrides(self):
        options = cli_worker.runtime_options('codex_cli', {'effort': 'xhigh', 'fast': True,
            'context_window': 200000, 'autocompact_tokens': 160000})
        self.assertIn('model_reasoning_effort="xhigh"', options)
        self.assertIn('service_tier="fast"', options)
        self.assertIn('model_context_window=200000', options)
        self.assertIn('model_auto_compact_token_limit=160000', options)

    def test_fast_off_never_sends_an_undocumented_tier(self):
        self.assertNotIn('service_tier', ' '.join(cli_worker.runtime_options('codex_cli', {'fast': False})))
        self.assertNotIn('service_tier', ' '.join(cli_worker.runtime_options('codex_cli')))

    def test_claude_maps_effort_and_native_autocompact_window(self):
        options = cli_worker.runtime_options('claude_cli', {'effort': 'high', 'fast': False,
            'context_window': 200000, 'autocompact_tokens': 160000})
        self.assertEqual(options, ['--effort', 'high', '--autocompact', '160000'])
        with self.assertRaisesRegex(ValueError, '100000'):
            cli_worker.runtime_options('claude_cli', {'autocompact_tokens': 50000})

    def test_invalid_runtime_cannot_inject_cli_arguments(self):
        for value in ({'effort': 'high --dangerously-bypass-approvals-and-sandbox'},
                      {'autocompact_tokens': '1; touch /tmp/bad'}, {'fast': 'true'},
                      {'context_window': True}, {'context_window': 100000, 'autocompact_tokens': 120000}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cli_worker.runtime_options('codex_cli', value)


if __name__ == '__main__':
    unittest.main()


class InstalledFlagTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cli_worker._HELP_CACHE.clear()

    def fake_cli(self, help_text):
        path = Path(self.temp.name) / 'claude'
        path.write_text('#!/bin/sh\ncat <<\'EOF\'\n' + help_text + '\nEOF\n', encoding='utf-8')
        path.chmod(0o755)
        return str(path)

    def test_option_missing_from_installed_version_is_refused(self):
        executable = self.fake_cli('  --effort <level>   Effort level')
        with patch.object(cli_worker.shutil, 'which', return_value=executable):
            self.assertEqual(cli_worker.runtime_options('claude_cli', {'effort': 'high'}), ['--effort', 'high'])
            with self.assertRaisesRegex(cli_worker.CliRuntimeError, '--autocompact'):
                cli_worker.runtime_options('claude_cli', {'autocompact_tokens': 200000})

    def test_unreadable_help_does_not_invent_a_refusal(self):
        with patch.object(cli_worker.shutil, 'which', return_value=str(Path(self.temp.name) / 'missing')):
            self.assertIsNone(cli_worker.installed_flags('claude_cli'))
            self.assertEqual(cli_worker.runtime_options('claude_cli', {'autocompact_tokens': 200000}),
                             ['--autocompact', '200000'])
