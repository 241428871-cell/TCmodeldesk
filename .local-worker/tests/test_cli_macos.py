"""Offline contracts for macOS CLI adapters; no real model invocation."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain_mcp_server
import cli_worker
import execution_endpoints
import execution_engines
import folder_picker
import goal_brain_runner
import model_registry as registry
import model_router
import native_write_sandbox
import panel_service
import workflow_settings


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.enterContext(patch.object(registry, 'HERE', self.root / 'desk'))
        self.enterContext(patch.object(registry, 'REGISTRY', self.root / 'desk' / 'models.json'))
        self.enterContext(patch.object(registry, 'RUNS', self.root / 'desk' / 'runs'))

    def test_disposable_copy_excludes_control_records_homes_and_nested_worktrees(self):
        source = self.root / 'source'
        source.mkdir()
        for name in ('.model-desk', '.local-worker', '.omo', '.venv', '.codex', '.claude', '.aws'):
            directory = source / name
            directory.mkdir()
            (directory / 'private-marker').write_text('fixture-only', encoding='utf-8')
        (source / 'app.py').write_text('value = 1\n', encoding='utf-8')
        destination = self.root / 'copy'
        shutil.copytree(source, destination, ignore=cli_worker._exclude)
        self.assertEqual([path.name for path in destination.iterdir()], ['app.py'])

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS Seatbelt contract')
    def test_claude_native_launch_adds_real_workspace_write_boundary(self) -> None:
        workspace = self.root / 'workspace'
        scratch = self.root / 'capture' / 'native-tmp'
        workspace.mkdir()
        scratch.parent.mkdir()

        protected = native_write_sandbox.protect(native_write_sandbox.ProtectRequest(
            engine='claude_cli', argv=('/fixture/claude', '-p'),
            workspace=workspace, scratch=scratch
        ))

        self.assertEqual(protected.mode, 'macos-seatbelt-workspace-write')
        self.assertEqual(protected.argv[0], '/usr/bin/env')
        self.assertIn('/usr/bin/sandbox-exec', protected.argv)
        profile = protected.argv[protected.argv.index('-p') + 1]
        self.assertIn(str(workspace.resolve()), profile)
        self.assertIn(f'/private/tmp/claude-{os.getuid()}', profile)
        self.assertNotIn(str(self.root / 'outside'), profile)
        self.assertIn('DISABLE_AUTOUPDATER=1', protected.argv)
        self.assertTrue(scratch.is_dir())

    def test_codex_keeps_its_native_sandbox_command(self) -> None:
        workspace = self.root / 'workspace'
        workspace.mkdir()
        original = ('/fixture/codex', 'exec')

        protected = native_write_sandbox.protect(native_write_sandbox.ProtectRequest(
            engine='codex_cli', argv=original, workspace=workspace,
            scratch=self.root / 'unused'
        ))

        self.assertEqual(protected.argv, original)
        self.assertEqual(protected.mode, 'codex-native-sandbox')
        self.assertFalse((self.root / 'unused').exists())

    def test_native_account_never_accepts_api_key_or_other_engine(self):
        row = registry.upsert_model({'provider': 'codex_cli', 'name': 'Codex', 'model': 'gpt-6.1-sol'})
        self.assertFalse(row['api_key_set'])
        self.assertEqual(row['default_agent'], 'codex_cli')
        with self.assertRaises(ValueError):
            registry.upsert_model({'provider': 'codex_cli', 'name': 'Codex', 'model': 'gpt-6.1-sol',
                                   'api_key': 'forbidden'})
        with self.assertRaises(ValueError):
            execution_engines.validate_model('dsh', row)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS Keychain and picker contract')
    def test_macos_keychain_reference_and_folder_picker(self):
        with patch('keyring.backends.macOS.Keyring') as keyring:
            reference = registry.protect('fixture-secret')
            self.assertTrue(reference.startswith('keychain:'))
            keyring.return_value.get_password.return_value = 'fixture-secret'
            self.assertEqual(registry.protect(reference, decrypt=True), 'fixture-secret')
        project = self.root / 'selected'
        project.mkdir()
        with patch.object(folder_picker.subprocess, 'run', return_value=subprocess.CompletedProcess(
                ['osascript'], 0, stdout=str(project) + '\n', stderr='')) as invoke:
            self.assertEqual(folder_picker.native_pick()['project'], str(project.resolve()))
            self.assertEqual(invoke.call_args.args[0][0], 'osascript')

    def test_cli_is_opt_in_and_cost_unknown_gate_is_closed(self):
        row = registry.upsert_model({'provider': 'claude_cli', 'name': 'Claude', 'model': 'sonnet'})
        self.assertFalse(execution_endpoints.snapshot()['endpoints']['claude_cli']['enabled'])
        execution_endpoints.update('claude_cli', {'enabled': True}, workflow_settings.DEFAULTS)
        request = {'kind': 'chat', 'prompt': 'explain', 'route_mode': 'manual', 'model_id': row['id']}
        base = {**workflow_settings.DEFAULTS, 'paid_allowed': True,
                'enforce_model_call_limit': False}
        with self.assertRaisesRegex(ValueError, '金额上限'):
            model_router.choose(request, registry.list_models()['models'], base)
        with self.assertRaisesRegex(ValueError, '付费执行'):
            model_router.choose(request, registry.list_models()['models'],
                                {**base, 'paid_allowed': False, 'enforce_paid_budget': False})
        selected = model_router.choose(request, registry.list_models()['models'],
                                       {**base, 'enforce_paid_budget': False})
        self.assertEqual(selected['engine'], 'claude_cli')
        self.assertIsNone(selected['estimated_cost_cny'])

    def test_only_declared_files_are_copied_back(self):
        project = self.root / 'original'
        project.mkdir()
        (project / 'allowed.txt').write_text('before', encoding='utf-8')
        (project / 'other.txt').write_text('keep', encoding='utf-8')
        (project / '.model-desk').mkdir()
        (project / '.model-desk' / 'secret.txt').write_text('record', encoding='utf-8')
        run = self.root / 'run'
        run.mkdir()
        (run / 'prompt.txt').write_text('change allowed', encoding='utf-8')
        state = {'engine': 'codex_cli', 'kind': 'edit', 'project': str(project),
                 'files': ['allowed.txt'], 'timeout': 30}
        script = ("import sys;from pathlib import Path;sys.stdin.read();"
                  "sys.exit(19) if Path('.model-desk').exists() else None;"
                  "Path('allowed.txt').write_text('after');Path('other.txt').write_text('bad');"
                  "print('done')")
        with patch.object(cli_worker, 'command', return_value=[sys.executable, '-c', script]), \
             patch.object(cli_worker.workflow_settings, 'snapshot', return_value={
                 **workflow_settings.DEFAULTS, 'paid_allowed': True, 'enforce_paid_budget': False,
                 'enforce_model_call_limit': False}), \
             patch.object(cli_worker.execution_endpoints, 'gate'), \
             patch.object(registry, 'patch_task'):
            cli_worker.run_task(run, state, {'provider': 'codex_cli', 'model': 'test'})
        self.assertEqual((project / 'allowed.txt').read_text(), 'after')
        self.assertEqual((project / 'other.txt').read_text(), 'keep')
        self.assertEqual((project / '.model-desk' / 'secret.txt').read_text(), 'record')

    def test_read_only_chat_can_use_empty_temporary_project(self):
        run = self.root / 'chat-run'
        run.mkdir()
        (run / 'prompt.txt').write_text('Explain this', encoding='utf-8')
        script = ("import sys;from pathlib import Path;sys.stdin.read();"
                  "sys.exit(19) if list(Path('.').iterdir()) else None;"
                  "print('{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
                  "\"session_id\":\"fixture-session\",\"result\":\"answer\"}')")
        with patch.object(cli_worker, 'command', return_value=[sys.executable, '-c', script]), \
             patch.object(cli_worker.workflow_settings, 'snapshot', return_value={
                 **workflow_settings.DEFAULTS, 'paid_allowed': True, 'enforce_paid_budget': False,
                 'enforce_model_call_limit': False}), \
             patch.object(cli_worker.execution_endpoints, 'gate'), \
             patch.object(registry, 'patch_task'):
            cli_worker.run_task(run, {'engine': 'claude_cli', 'kind': 'chat', 'project': '',
                                      'files': [], 'timeout': 0},
                                {'provider': 'claude_cli', 'model': 'sonnet'})

    @unittest.skipIf(sys.platform == 'win32', 'Windows symlinks require developer privileges')
    def test_copyback_rechecks_record_alias_changed_during_execution(self) -> None:
        project = self.root / 'alias-project'
        source = project / 'source'
        protected = project / '.model-desk'
        source.mkdir(parents=True)
        protected.mkdir()
        (source / 'allowed.txt').write_text('before')
        (protected / 'allowed.txt').write_text('protected record')
        (project / 'alias').symlink_to(source, target_is_directory=True)
        run = self.root / 'alias-run'
        run.mkdir()
        (run / 'prompt.txt').write_text('Fixture edit')
        script = ("import sys;from pathlib import Path;sys.stdin.read();"
                  "Path('alias').mkdir();Path('alias/allowed.txt').write_text('changed');"
                  "original=Path(sys.argv[1]);original.unlink();"
                  "original.symlink_to(sys.argv[2],target_is_directory=True);print('done')")
        args = [sys.executable, '-c', script, str(project / 'alias'), str(protected)]
        with patch.object(cli_worker, 'command', return_value=args), \
             patch.object(workflow_settings, 'snapshot', return_value={
                 **workflow_settings.DEFAULTS, 'paid_allowed': True, 'enforce_paid_budget': False,
                 'enforce_model_call_limit': False}), \
             patch.object(execution_endpoints, 'gate'), patch.object(registry, 'patch_task'), \
             self.assertRaises(ValueError):
            cli_worker.run_task(run, {'engine': 'codex_cli', 'kind': 'edit', 'project': str(project),
                                     'files': ['alias/allowed.txt'], 'timeout': 30},
                                {'provider': 'codex_cli', 'model': 'fixture'})
        self.assertEqual((protected / 'allowed.txt').read_text(), 'protected record')

    def test_claude_main_brain_uses_existing_host_neutral_mcp(self):
        project = self.root / 'brain-project'
        project.mkdir()
        args = brain_mcp_server.parse_args([
            '--project', str(project), '--host', 'claude', '--owner', 'claude-main',
            '--role', 'brain', '--session-binding', 'user_bound',
            '--session-id', 'operator-bound-session'])
        with patch.object(brain_mcp_server.brain_transport, 'normalize_base_url', return_value='http://127.0.0.1:8765'):
            brain_mcp_server.configure(args)
        self.assertEqual(brain_mcp_server._CONTEXT.host, 'claude')
        self.assertIn(brain_mcp_server.TOOL_DISPATCH, brain_mcp_server.available_tools())

    def test_claude_brain_reads_structured_final_result_after_streamed_tool_events(self) -> None:
        actions = {'plan': None, 'questions': [], 'dispatch_stage_ids': [], 'reviews': [],
                   'obligations': [], 'complete': False, 'summary': 'Verified current inputs'}
        stream = [
            {'type': 'system', 'subtype': 'init', 'session_id': 'fixture-session'},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Read'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False,
             'session_id': 'fixture-session', 'structured_output': actions},
        ]
        script = 'import sys;sys.stdin.read();print(' + repr('\n'.join(json.dumps(row) for row in stream)) + ')'
        with patch.object(goal_brain_runner, 'command', return_value=(
                [sys.executable, '-c', script], 'fixture-session')):
            result = goal_brain_runner.run({'project': str(self.root), 'goal_id': 'fixture-goal'},
                'claude_cli', 'fixture', {},
                self.root / 'native-round', lambda *_args: None, lambda: False)
        self.assertEqual(result['actions'], actions)
        self.assertEqual(result['native_session_id'], 'fixture-session')

    def _brain_with_mode(self, reported: str, directory: str):
        actions = {'plan': None, 'questions': [], 'dispatch_stage_ids': [], 'reviews': [],
                   'obligations': [], 'complete': False, 'summary': 'Checked'}
        stream = [
            {'type': 'system', 'subtype': 'init', 'session_id': 'fixture-session',
             'model': 'claude-fixture', 'permissionMode': reported, 'claude_code_version': '2.1.309'},
            {'type': 'result', 'subtype': 'success', 'is_error': False,
             'session_id': 'fixture-session', 'structured_output': actions},
        ]
        script = 'import sys;sys.stdin.read();print(' + repr('\n'.join(json.dumps(row) for row in stream)) + ')'
        with patch.object(goal_brain_runner, 'command', return_value=(
                [sys.executable, '-c', script], 'fixture-session')):
            return goal_brain_runner.run({'project': str(self.root), 'goal_id': 'fixture-goal'},
                'claude_cli', 'fixture', {},
                self.root / directory, lambda *_args: None, lambda: False, permission_mode='auto')

    def test_claude_brain_records_effective_auto_mode(self) -> None:
        self._brain_with_mode('auto', 'effective-auto')
        effective = json.loads((self.root / 'effective-auto' / 'effective.json').read_text(encoding='utf-8'))
        self.assertEqual(effective, {'model': 'claude-fixture', 'permission_mode': 'auto', 'version': '2.1.309'})

    def test_claude_brain_refuses_a_widened_permission_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, '实际权限模式'):
            self._brain_with_mode('bypassPermissions', 'effective-bypass')

    def test_claude_zero_exit_without_terminal_result_cannot_write_back_changes(self) -> None:
        project = self.root / 'partial-project'
        project.mkdir()
        target = project / 'allowed.txt'
        target.write_text('original content')
        run = self.root / 'partial-run'
        run.mkdir()
        (run / 'prompt.txt').write_text('Fixture edit')
        script = ("import sys;from pathlib import Path;sys.stdin.read();"
                  "Path('allowed.txt').write_text('unproven change');"
                  "print('{\"type\":\"assistant\",\"message\":{\"content\":\"incomplete\"}}')")
        with patch.object(cli_worker, 'command', return_value=[sys.executable, '-c', script]), \
             patch.object(workflow_settings, 'snapshot', return_value={
                 **workflow_settings.DEFAULTS, 'paid_allowed': True, 'enforce_paid_budget': False,
                 'enforce_model_call_limit': False}), \
             patch.object(execution_endpoints, 'gate'), patch.object(registry, 'patch_task'), \
             self.assertRaisesRegex(ValueError, 'claude_result_missing'):
            cli_worker.run_task(run, {'engine': 'claude_cli', 'kind': 'edit', 'project': str(project),
                                     'files': ['allowed.txt'], 'timeout': 30},
                                {'provider': 'claude_cli', 'model': 'fixture'})
        self.assertEqual(target.read_text(), 'original content')

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS launcher contract')
    def test_cold_start_uses_python_launcher_not_powershell(self):
        with patch.object(panel_service, 'workspace', side_effect=[None, str(panel_service.ROOT)]), \
             patch.object(panel_service.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as start:
            result = panel_service.ensure_service('http://127.0.0.1:8765')
        self.assertTrue(result['ready'])
        command = start.call_args.args[0]
        self.assertEqual(Path(command[1]).name, 'start_panel.py')
        self.assertNotIn('pwsh', ' '.join(command))


if __name__ == '__main__':
    unittest.main()
