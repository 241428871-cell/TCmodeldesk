"""Project model configuration contracts with isolated records and no providers."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import execution_endpoints
import model_registry as registry
import panel_server as panel
import platform_runtime
import project_defaults
import provider_accounts
import stage_store
import workflow_settings
import workspace_config as workspace
import workspace_status


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.project = self.root / 'project'
        self.other = self.root / 'other'
        self.project.mkdir()
        self.other.mkdir()
        self.enterContext(patch.object(registry, 'HERE', self.root / 'desk'))
        self.enterContext(patch.object(registry, 'REGISTRY', self.root / 'desk' / 'models.json'))
        self.enterContext(patch.object(registry, 'RUNS', self.root / 'desk' / 'runs'))
        self.enterContext(patch.object(workspace.project_views, 'project_bookmarks', return_value=[]))
        self.public_agents = workspace_status.execution_engines.public_agents
        self.enterContext(patch.object(workspace_status.execution_engines, 'public_agents', return_value={
            'codex_cli': {'ready': True}, 'claude_cli': {'ready': True}, 'dsh': {'ready': False}}))
        self.enterContext(patch.object(workspace_status.execution_endpoints, 'snapshot', return_value={
            'readable': True, 'endpoints': {'codex_cli': {'enabled': True}, 'claude_cli': {'enabled': True}}}))
        self.model = registry.upsert_model({'provider': 'codex_cli', 'name': 'Codex', 'model': 'fixture-model'})

    def register(self, project=None):
        return workspace.update_project({'action': 'add', 'path': str(project or self.project)})

    def bind(self, **values):
        return workspace.update_binding({'project': str(self.project), 'model_id': self.model['id'], **values})

    def test_saved_project_defaults_clone_new_bindings_without_copying_records_or_secrets(self):
        self.register()
        self.bind(role='decision', is_default=True, effort='xhigh', context_window=1000000, autocompact_tokens=450000)
        self.bind(role='execution', is_default=True, effort='low', context_window=500000, autocompact_tokens=250000,
                  max_instances=2, collaboration_hint='Review changes independently')
        workspace.update_project({'action': 'concurrency', 'path': str(self.project), 'work_concurrency': 16})
        before = (self.root / 'desk/workspace.json').read_bytes()
        model_before = registry.REGISTRY.read_bytes()
        with patch('dsh_metadata.probe', side_effect=AssertionError('unexpected model probe')):
            saved = project_defaults.capture(str(self.project))
            created = workspace.update_project({'action': 'add', 'path': str(self.other)})
        self.assertEqual(saved['model_count'], 2)
        self.assertEqual(saved['work_concurrency'], 16)
        old, new = created['projects']
        self.assertEqual(json.loads(before)['projects'][0], old)
        self.assertEqual(new['work_concurrency'], 16)
        self.assertEqual([{k:v for k,v in b.items() if k!='id'} for b in old['bindings']],
                         [{k:v for k,v in b.items() if k!='id'} for b in new['bindings']])
        self.assertTrue({b['id'] for b in old['bindings']}.isdisjoint(b['id'] for b in new['bindings']))
        self.assertFalse((self.other / '.model-desk').exists())
        self.assertEqual(registry.REGISTRY.read_bytes(), model_before)
        template = json.loads((registry.HERE / project_defaults.NAME).read_bytes())
        self.assertEqual(set(template), {'schema', 'updated_at', 'work_concurrency', 'bindings'})
        self.assertTrue(all(set(row).isdisjoint({'id','secret','api_key','account_id','project','path'}) for row in template['bindings']))

    def test_new_default_template_does_not_rewrite_existing_projects(self):
        self.register()
        self.bind(role='decision', effort='high')
        self.register(self.other)
        project_defaults.capture(str(self.project))
        existing = workspace.update_project({'action': 'add', 'path': str(self.other)})['projects'][1]
        self.assertEqual(existing['bindings'], [])
        self.assertEqual(existing['work_concurrency'], 8)

    def test_default_template_does_not_switch_to_replacement_model(self):
        self.register()
        self.bind(role='decision')
        project_defaults.capture(str(self.project))
        with registry.registry_lock():
            data = registry.read_registry()
            data['models'][0]['model'] = 'different-model'
            registry.write_json(registry.REGISTRY, data)
        before = (registry.HERE / 'workspace.json').read_bytes()
        with self.assertRaises(project_defaults.DefaultsError):
            workspace.update_project({'action': 'add', 'path': str(self.other)})
        self.assertEqual((registry.HERE / 'workspace.json').read_bytes(), before)

    def test_corrupt_default_template_blocks_new_project_but_not_existing_snapshot(self):
        self.register()
        registry.write_json(registry.HERE / project_defaults.NAME, {'work_concurrency': True})
        self.assertFalse(workspace.snapshot()['project_defaults']['readable'])
        before = (registry.HERE / 'workspace.json').read_bytes()
        with self.assertRaises(ValueError):
            workspace.update_project({'action': 'add', 'path': str(self.other)})
        self.assertEqual((registry.HERE / 'workspace.json').read_bytes(), before)

    def test_bookmarked_project_adoption_inherits_defaults_once(self):
        self.register()
        self.bind(role='decision', effort='xhigh')
        project_defaults.capture(str(self.project))
        with patch.object(workspace.project_views, 'project_bookmarks', return_value=[{'path': str(self.other)}]):
            created = workspace.update_project({'action': 'add', 'path': str(self.other)})['projects'][1]
        self.assertEqual(created['bindings'][0]['effort'], 'xhigh')
        self.assertTrue(created['managed'])

    def test_project_model_keeps_thinking_capabilities_without_probing_or_rewriting(self):
        self.register()
        model = registry.upsert_model({'provider': 'deepseek', 'name': 'Official Flash',
            'model': 'deepseek-flash', 'api_base': 'https://api.deepseek.com',
            'default_agent': 'dsh'})
        self.bind(model_id=model['id'], role='execution')
        before = registry.REGISTRY.read_bytes()
        with patch('thinking_config.probe', side_effect=AssertionError('unexpected probe')):
            binding = workspace.snapshot()['projects'][0]['bindings'][0]
        self.assertEqual(binding['model']['thinking']['supported'], ['default', 'off', 'low', 'high', 'max'])
        self.assertEqual(binding['model']['thinking']['default_effort'], 'high')
        self.assertEqual(registry.REGISTRY.read_bytes(), before)

    def test_project_native_dsh_capabilities_follow_exact_cached_route(self):
        self.register()
        ident = '["deepseek-account","deepseek-flash"]'
        model = registry.upsert_model({'provider': 'dsh', 'name': 'Native Flash',
            'model': ident, 'default_agent': 'dsh'})
        self.bind(model_id=model['id'], role='execution')
        with patch.dict(provider_accounts._CATALOGS, {'dsh': {'models': [
                {'id': ident, 'effort_levels': ['off', 'high']},
                {'id': '["deepseek-official","deepseek-flash"]', 'effort_levels': ['low', 'max']}]}}, clear=True), \
                patch('dsh_metadata.probe', side_effect=AssertionError('unexpected CLI launch')):
            binding = workspace.snapshot()['projects'][0]['bindings'][0]
        self.assertEqual(binding['model']['thinking']['supported'], ['default', 'off', 'high'])

    def test_enforcement_is_role_specific_and_does_not_dispatch(self) -> None:
        self.register()
        self.model = registry.upsert_model({'provider': 'claude_cli', 'name': 'Claude', 'model': 'fixture'})
        self.bind(role='decision')
        self.bind(role='execution')
        workflow_settings.snapshot()
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob('*') if path.is_file()}
        with patch.object(panel.subprocess, 'Popen') as launch:
            data = workspace.snapshot()
        declarations = {row['role']: row['enforcement'] for row in data['projects'][0]['bindings']}
        expected = 'native_sandbox' if sys.platform == 'darwin' else 'controlled_tools'
        worker_expected = 'native_sandbox' if sys.platform == 'darwin' else 'dispatch_only'
        self.assertEqual(declarations['decision']['enforcement_scope'], expected)
        self.assertEqual(declarations['execution']['enforcement_scope'], worker_expected)
        self.assertFalse(declarations['decision']['runtime_verified'])
        self.assertEqual(before, {str(path.relative_to(self.root)): path.read_bytes()
                                 for path in self.root.rglob('*') if path.is_file()})
        launch.assert_not_called()

    def test_public_agents_declare_every_execution_adapter_without_runtime_proof(self) -> None:
        import execution_engines
        with patch.object(execution_engines, 'availability', return_value={'ready': True}):
            agents = self.public_agents()
            self.assertEqual(set(agents), set(execution_engines.AGENTS))
            for engine in execution_engines.AGENTS:
                declared = agents[engine]['enforcement']
                self.assertNotEqual(declared['enforcement_scope'], 'unsupported')
                self.assertFalse(declared['runtime_verified'])

    def test_empty_snapshot_has_no_side_effects_or_implicit_project(self):
        data = workspace.snapshot()
        self.assertEqual(data['projects'], [])
        self.assertIsNone(data['selected_project'])
        self.assertFalse((registry.HERE / workspace.CONFIG_NAME).exists())

    def test_concurrency_rejects_non_integer_public_inputs(self):
        self.register()
        for value in ('2', None, True, 2.5):
            with self.subTest(value=value), self.assertRaises(workspace.WorkspaceError):
                workspace.update_project({'action': 'concurrency', 'path': str(self.project),
                                          'work_concurrency': value})
        self.assertEqual(workspace.work_concurrency(str(self.project)), 8)

    def test_waiting_reasons_are_scoped_to_their_own_project(self):
        import execution_leases
        from model_desk_control.resource_leases import LeaseState

        self.register()
        tasks = [{'id': 'mine', 'title': 'My task', 'project': str(self.project)},
                 {'id': 'other', 'title': 'Other task', 'project': str(self.other)}]
        with patch.object(execution_leases, 'state', return_value=LeaseState(
                held=(), waiting=(('mine', '项目并发已满'), ('other', '账户并发已满')))):
            snapshot = workspace.snapshot(tasks)
        self.assertEqual(snapshot['projects'][0]['waiting_tasks'],
                         [{'task_id': 'mine', 'title': 'My task', 'reason': '项目并发已满'}])

    def test_folder_registration_alias_rename_and_selection_are_persistent(self):
        self.register()
        workspace.update_project({'action': 'rename', 'path': str(self.project), 'name': '论文工作区'})
        data = workspace.snapshot()
        self.assertEqual(data['selected_project'], str(self.project))
        self.assertEqual(data['projects'][0]['name'], '论文工作区')
        self.assertTrue(data['projects'][0]['available'])
        self.assertFalse((self.project / '.model-desk').exists())

    def test_removing_project_forgets_registration_without_deleting_contents(self):
        self.register()
        evidence = self.project / 'important.txt'
        evidence.write_text('preserve', encoding='utf-8')
        workspace.update_project({'action': 'remove', 'path': str(self.project)})
        self.assertEqual(workspace.snapshot()['projects'], [])
        self.assertEqual(evidence.read_text(encoding='utf-8'), 'preserve')
        self.assertIsNone(workspace.snapshot()['selected_project'])

    def test_removed_legacy_bookmark_does_not_reappear(self):
        with patch.object(workspace.project_views, 'project_bookmarks', return_value=[{'path': str(self.project)}]):
            self.assertEqual(len(workspace.snapshot()['projects']), 1)
            workspace.update_project({'action': 'remove', 'path': str(self.project)})
            self.assertEqual(workspace.snapshot()['projects'], [])

    def test_role_associations_are_separate_and_defaults_are_native_valid(self):
        self.register()
        self.bind(role='decision')
        data = self.bind(role='execution')
        bindings = data['projects'][0]['bindings']
        self.assertEqual({row['role'] for row in bindings}, {'decision', 'execution'})
        self.assertEqual(bindings[0]['context_window'], 200000)
        self.assertEqual(bindings[0]['autocompact_tokens'], 160000)
        self.assertEqual(bindings[0]['effort'], 'medium')
        self.assertFalse(bindings[0]['fast'])

    def test_runtime_save_never_reads_api_credentials(self):
        self.register()
        with patch.object(registry, 'get_model', side_effect=AssertionError('must not retrieve key')):
            data = self.bind(role='execution', effort='high', fast=True)
        saved = data['projects'][0]['bindings'][0]
        self.assertEqual(saved['effort'], 'high')
        self.assertTrue(saved['fast'])
        self.assertEqual(registry.list_models()['models'][0]['id'], self.model['id'])

    def test_dispatch_does_not_adopt_another_projects_or_decision_binding(self):
        self.register()
        binding = self.bind(role='decision')['projects'][0]['bindings'][0]
        with self.assertRaises(workspace.WorkspaceError):
            workspace.dispatch_binding(str(self.project), self.model['id'], binding['id'])
        with self.assertRaises(workspace.WorkspaceError):
            workspace.dispatch_binding(str(self.other), self.model['id'], binding['id'])
        self.assertIsNone(workspace.dispatch_binding(str(self.other), self.model['id']))

    def test_disabled_binding_rejects_new_dispatch_but_keeps_running_status(self):
        self.register()
        binding = self.bind(role='execution')['projects'][0]['bindings'][0]
        self.bind(action='disable', binding_id=binding['id'])
        with self.assertRaises(workspace.WorkspaceError):
            workspace.dispatch_binding(str(self.project), self.model['id'])
        data = workspace.snapshot([{'project': str(self.project), 'model_id': self.model['id'], 'status': 'running'}])
        public = data['projects'][0]['bindings'][0]
        self.assertEqual(public['status'], 'running')
        self.assertFalse(public['enabled'])
        self.assertEqual(public['running_count'], 1)

    def test_enabling_is_not_claimed_as_running_and_decision_never_borrows_executor(self):
        self.register()
        self.bind(role='decision')
        self.bind(role='execution')
        idle = workspace.snapshot()['projects'][0]['bindings']
        self.assertTrue(all(row['status'] == 'ready' for row in idle))
        running = workspace.snapshot([{'project': str(self.project), 'model_id': self.model['id'], 'status': 'running'}])
        self.assertEqual(running['projects'][0]['bindings'][0]['running_count'], 0)
        self.assertEqual(running['projects'][0]['bindings'][1]['running_count'], 1)

    def test_decision_binding_uses_durable_brain_launch_receipts(self):
        self.register()
        self.bind(role='decision')
        records = self.project / '.model-desk' / 'records.db'
        records.parent.mkdir()
        records.touch()
        goals = [{'id': 'starting'}, {'id': 'running'}, {'id': 'unknown'}, {'id': 'other-model'}]
        clocks = {
            'starting': {'active_intent': {'status': 'claimed', 'brain_model_id': self.model['id']}},
            'running': {'active_intent': {'status': 'started', 'brain_model_id': self.model['id']}},
            'unknown': {'active_intent': {'status': 'unknown', 'brain_model_id': self.model['id']}},
            'other-model': {'active_intent': {'status': 'started', 'brain_model_id': 'different'}},
        }
        with patch('stage_store.list_goals', return_value=goals), patch(
                'continuation_clock.snapshot', side_effect=lambda _project, goal: clocks[goal]):
            binding = workspace.snapshot()['projects'][0]['bindings'][0]
        self.assertEqual(binding['running_count'], 1)
        self.assertEqual(binding['starting_count'], 1)
        self.assertEqual(binding['unknown_count'], 1)
        self.assertEqual(binding['status'], 'running')

    def test_unknown_brain_receipt_is_not_presented_as_idle_or_ready(self):
        self.register()
        self.bind(role='decision')
        records = self.project / '.model-desk' / 'records.db'
        records.parent.mkdir()
        records.touch()
        with patch('stage_store.list_goals', return_value=[{'id': 'goal'}]), patch(
                'continuation_clock.snapshot', return_value={
                    'active_intent': {'status': 'unknown', 'brain_model_id': self.model['id']}}):
            binding = workspace.snapshot()['projects'][0]['bindings'][0]
        self.assertEqual(binding['running_count'], 0)
        self.assertEqual(binding['unknown_count'], 1)
        self.assertEqual(binding['status'], 'unknown')

    def test_real_goal_receipt_marks_decision_model_started(self):
        self.register()
        self.bind(role='decision')
        goal = stage_store.create_goal(
            'Workspace activity', 'Show the durable brain state', str(self.project), store='project'
        )
        continuation_clock.configure_clock(
            str(self.project), goal['id'], interval_seconds=1800, enabled=True
        )
        _ = continuation_clock.control(
            str(self.project), goal['id'], 'start', {'model_id': self.model['id']}
        )
        claim = continuation_clock.claim_due(str(self.project), goal['id'], 'workspace-test')
        self.assertIsNotNone(claim)
        continuation_clock.record_runner(
            str(self.project), goal['id'], claim['id'], claim['authority_epoch'],
            os.getpid(), platform_runtime.process_birth(os.getpid())
        )
        binding = workspace.snapshot()['projects'][0]['bindings'][0]
        self.assertEqual(binding['running_count'], 1)
        self.assertEqual(binding['starting_count'], 0)
        self.assertEqual(binding['unknown_count'], 0)
        self.assertEqual(binding['status'], 'running')

    def test_runtime_validation_rejects_native_invalid_thresholds_and_bool_tokens(self):
        self.register()
        for values in ({'context_window': True}, {'autocompact_tokens': 200000}, {'effort': 'invented'}, {'fast': 'yes'}):
            with self.subTest(values=values), self.assertRaises(workspace.WorkspaceError):
                self.bind(role='execution', **values)
        claude = registry.upsert_model({'provider': 'claude_cli', 'name': 'Claude', 'model': 'sonnet'})
        for values in ({'autocompact_tokens': 32000}, {'fast': True}):
            with self.subTest(values=values), self.assertRaises(workspace.WorkspaceError):
                workspace.update_binding({'project': str(self.project), 'model_id': claude['id'], **values})

    def test_snapshot_is_detached_from_later_runtime_edits(self):
        self.register()
        binding = self.bind(role='execution', effort='high')['projects'][0]['bindings'][0]
        captured = workspace.dispatch_binding(str(self.project), self.model['id'])
        self.bind(binding_id=binding['id'], effort='low')
        self.assertEqual(captured['effort'], 'high')
        self.assertEqual(workspace.dispatch_binding(str(self.project), self.model['id'])['effort'], 'low')

    def test_codex_native_efforts_include_ultra_and_claude_rejects_foreign_levels(self):
        self.register()
        for effort in ('none', 'minimal', 'ultra'):
            with self.subTest(provider='codex_cli', effort=effort):
                self.bind(role='execution', effort=effort)
                self.assertEqual(workspace.dispatch_binding(str(self.project), self.model['id'])['effort'], effort)
        claude = registry.upsert_model({'provider': 'claude_cli', 'name': 'Claude', 'model': 'sonnet'})
        for effort in ('none', 'minimal', 'ultra', 'invented'):
            with self.subTest(provider='claude_cli', effort=effort), self.assertRaises(workspace.WorkspaceError):
                workspace.update_binding({'project': str(self.project), 'model_id': claude['id'], 'effort': effort})
        for effort in ('default', 'low', 'medium', 'high', 'xhigh', 'max'):
            with self.subTest(provider='claude_cli', effort=effort):
                workspace.update_binding({'project': str(self.project), 'model_id': claude['id'], 'effort': effort})

    def test_invalid_paths_and_unknown_roles_do_not_write(self):
        for path in ('relative', 'https://example.test', str(self.root / 'missing')):
            with self.subTest(path=path), self.assertRaises(ValueError):
                workspace.update_project({'action': 'add', 'path': path})
        self.register()
        with self.assertRaises(workspace.WorkspaceError):
            self.bind(role='unknown')

    def test_each_role_has_one_default_and_only_execution_default_routes(self):
        self.register()
        first = self.bind(role='execution')['projects'][0]['bindings'][0]
        other = registry.upsert_model({'provider': 'codex_cli', 'name': 'Other', 'model': 'other-model'})
        workspace.update_binding({'project': str(self.project), 'model_id': other['id'],
                                  'role': 'execution', 'is_default': True})
        data = workspace.snapshot()['projects'][0]['bindings']
        self.assertFalse(next(b for b in data if b['id'] == first['id'])['is_default'])
        self.assertEqual(workspace.default_model(str(self.project)), other['id'])
        self.bind(role='decision', is_default=True)
        self.assertEqual(workspace.default_model(str(self.project)), other['id'])

    def test_project_routing_uses_only_enabled_execution_bindings(self):
        models = registry.list_models()['models']
        self.assertEqual(workspace.execution_models(str(self.project), models), models)
        self.register()
        self.assertEqual(workspace.execution_models(str(self.project), models), [])
        self.bind(role='decision')
        self.assertEqual(workspace.execution_models(str(self.project), models), [])
        binding = self.bind(role='execution')['projects'][0]['bindings'][-1]
        self.assertEqual(workspace.execution_models(str(self.project), models), models)
        self.bind(action='disable', binding_id=binding['id'])
        self.assertEqual(workspace.execution_models(str(self.project), models), [])

    def test_managed_project_stays_closed_after_last_binding_is_removed(self):
        self.register()
        binding = self.bind(role='execution')['projects'][0]['bindings'][0]
        self.bind(action='remove', binding_id=binding['id'])
        self.assertTrue(workspace.snapshot()['projects'][0]['managed'])
        self.assertEqual(workspace.execution_models(str(self.project), registry.list_models()['models']), [])

    def test_legacy_bookmarks_remain_legacy_when_other_project_is_saved(self):
        models = registry.list_models()['models']
        with patch.object(workspace.project_views, 'project_bookmarks', return_value=[{'path': str(self.other)}]):
            self.assertFalse(workspace.snapshot()['projects'][0]['managed'])
            self.register()
        legacy = next(p for p in workspace.snapshot()['projects'] if p['path'] == str(self.other))
        self.assertFalse(legacy['managed'])
        self.assertEqual(workspace.execution_models(str(self.other), models), models)
        workspace.update_project({'action': 'add', 'path': str(self.other)})
        self.assertEqual(workspace.execution_models(str(self.other), models), [])

    def test_shared_dispatch_snapshots_project_runtime_without_starting_worker(self):
        self.register()
        binding = self.bind(role='execution', effort='high', fast=True)['projects'][0]['bindings'][0]
        settings = {**workflow_settings.DEFAULTS, 'enabled': True, 'paid_allowed': True,
                    'enforce_paid_budget': False, 'enforce_model_call_limit': False}
        execution_endpoints.update('codex_cli', {'enabled': True}, settings)
        with patch.object(panel.workflow_settings, 'snapshot', return_value=settings), \
             patch.object(panel.execution_engines, 'availability', return_value={'ready': True}), \
             patch.object(panel, 'public_tasks', return_value=[]), \
             patch.object(panel.EXECUTOR, 'submit') as start:
            result = panel.submit_task({'kind': 'chat', 'project': str(self.project), 'prompt': 'explain',
                                        'binding_id': binding['id']})
        self.assertEqual(result['model_id'], self.model['id'])
        self.assertEqual(result['workspace_binding_id'], binding['id'])
        self.assertEqual(result['cli_runtime']['effort'], 'high')
        self.assertTrue(result['cli_runtime']['fast'])
        self.assertEqual(start.call_count, 1)
        self.bind(binding_id=binding['id'], effort='low')
        self.assertEqual(registry.read_task(registry.task_dir(result['id']))['cli_runtime']['effort'], 'high')

    def test_http_catalog_and_refresh_are_explicit_while_workspace_get_is_cached(self):
        self.register()
        self.bind(role='decision', effort='xhigh')
        server = ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = build_opener(ProxyHandler({}))
        base = f'http://127.0.0.1:{server.server_port}'
        def request(path, body=None):
            req = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={'Content-Type': 'application/json', 'X-Panel-Token': panel.TOKEN})
            with opener.open(req, timeout=5) as response:
                return json.load(response)
        try:
            with patch.object(panel, 'public_tasks', return_value=[]), \
                 patch.object(provider_accounts, 'account_snapshot', return_value={'state': 'unknown'}) as account, \
                 patch.object(provider_accounts, 'catalog_snapshot', return_value={'models': []}) as cached, \
                 patch.object(provider_accounts, 'catalog', return_value={'models': []}) as catalog:
                result = request('/api/workspace')
                self.assertIn('projects', result)
                self.assertEqual(set(result['catalogs']), {'codex_cli', 'claude_cli', 'dsh'})
                self.assertEqual(cached.call_count, 3)
                self.assertTrue(all(not c.kwargs.get('force') for c in account.call_args_list))
                catalog.assert_not_called()
                saved = request('/api/workspace/defaults', {'project': str(self.project)})
                self.assertTrue(saved['configured'])
                self.assertEqual(saved['model_count'], 1)
                self.assertTrue(request('/api/workspace')['project_defaults']['configured'])
                catalog.assert_not_called()
                request('/api/provider-catalog', {'provider': 'codex_cli'})
                catalog.assert_called_once_with('codex_cli')
                request('/api/accounts/refresh', {'provider': 'claude_cli'})
                self.assertEqual(account.call_args.kwargs, {'force': True})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
