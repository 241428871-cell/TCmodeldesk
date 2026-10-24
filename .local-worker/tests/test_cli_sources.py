"""Backend selection uses isolated configuration and never launches a provider."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_sources
import cli_updates
import dsh_worker
import execution_endpoints as endpoints
import executor_adapter
import model_registry as registry
import panel_server
import preset_runtime
import provider_accounts
import runtime_paths


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, 'HERE', tmp_path)
    monkeypatch.setenv('MODEL_DESK_DSH_CLI', str(tmp_path / 'absent'))


def command(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o755)
    return path


def test_dsh_desktop_default_and_explicit_switch_survive_snapshot(tmp_path, monkeypatch):
    desktop = command(tmp_path / 'DSH.app/Contents/MacOS/DeepSeek Harness')
    bundled = command(desktop.parent.parent / 'Resources/runtime/cli/bin/dsh')
    external = command(tmp_path / 'npm/bin/dsh')
    monkeypatch.setattr(runtime_paths, 'dsh_desktop_executable', lambda: desktop)
    monkeypatch.setenv('MODEL_DESK_DSH_CLI', str(external))
    assert runtime_paths.dsh_command() == bundled
    before = endpoints.snapshot()['endpoints']
    endpoints.update('dsh', {'cli_source': 'standalone'}, {}, idle_guard=lambda: None)
    assert endpoints.snapshot()['endpoints']['dsh']['cli_source'] == 'standalone'
    assert runtime_paths.dsh_command() == external
    assert runtime_paths.dsh_executable() == external
    assert endpoints.snapshot()['endpoints']['pi'] == before['pi']
    endpoints.update('dsh', {'cli_source': 'desktop'}, {}, idle_guard=lambda: None)
    assert runtime_paths.dsh_command() == bundled


def test_desktop_link_is_not_an_independent_dsh(tmp_path, monkeypatch):
    bundled = command(tmp_path / 'DSH.app/Contents/Resources/runtime/cli/bin/dsh')
    link = tmp_path / 'dsh'
    link.symlink_to(bundled)
    monkeypatch.setenv('MODEL_DESK_DSH_CLI', str(link))
    assert cli_sources.standalone_dsh() is None
    with pytest.raises(ValueError, match='未安装'):
        endpoints.update('dsh', {'cli_source': 'standalone'}, {}, idle_guard=lambda: None)
    assert cli_sources.selected_source('dsh') == 'desktop'


@pytest.mark.parametrize('activity', [
    {'complete': False, 'tasks': [], 'goals': []},
    {'complete': True, 'tasks': [{'id': 'busy'}], 'goals': []},
    {'complete': True, 'tasks': [], 'goals': [{'id': 'brain'}]},
])
def test_source_mutation_requires_complete_idle_proof(tmp_path, monkeypatch, activity):
    external = command(tmp_path / 'dsh')
    monkeypatch.setenv('MODEL_DESK_DSH_CLI', str(external))
    with patch.object(panel_server, 'cli_update_activity', return_value=activity):
        with pytest.raises(ValueError):
            endpoints.update('dsh', {'cli_source': 'standalone'}, {}, idle_guard=panel_server.cli_source_idle_guard)
    assert not (tmp_path / endpoints.CONFIG_NAME).exists()


def test_corrupt_or_foreign_source_never_defaults_to_a_live_binary(tmp_path):
    for key, value in [('dsh', 'automatic'), ('claude_cli', 'desktop')]:
        registry.write_json(tmp_path / endpoints.CONFIG_NAME, {'schema': 1, 'endpoints': {
            key: {'enabled': True, 'current_model': None, 'cli_source': value}}})
        assert cli_sources.selected_source('dsh') is None
        assert runtime_paths.dsh_command() is None
    with pytest.raises(ValueError):
        endpoints.update('dsh', {'cli_source': 'desktop', 'enabled': True}, {})


def test_standalone_dsh_identity_presets_and_background_entry_do_not_need_desktop(tmp_path, monkeypatch):
    root = tmp_path / 'node_modules/@deepseek-ai/dsh'
    external = command(root / 'lib/bin.js')
    (root / 'package.json').write_text(json.dumps({
        'name': '@deepseek-ai/dsh', 'version': '0.2.1-rc.2', 'bin': {'dsh': 'lib/bin.js'}}))
    web = root.parent / 'dsh-web-app'
    (web / 'presets').mkdir(parents=True)
    (web / 'package.json').write_text('{"name":"@deepseek-ai/dsh-web-app"}')
    declaration = web / 'presets/standard.patch.yml'
    declaration.write_text('- insert:\n  - name: "@deepseek-ai/dsh-agent-preset"\n    config:\n      id: standard\n      plugins: []\n')
    (web / 'cordis.patch.yml').write_text('[]\n')
    monkeypatch.setenv('MODEL_DESK_DSH_CLI', str(external))
    monkeypatch.setattr(runtime_paths, 'dsh_desktop_executable', lambda: None)
    endpoints.update('dsh', {'cli_source': 'standalone'}, {}, idle_guard=lambda: None)
    assert executor_adapter.installed_executor_version() == 'standalone:0.2.1-rc.2'
    preset = preset_runtime.load({'provider': 'deepseek'}, external)
    assert preset['source'] == str(declaration)
    assert preset['revision'] == preset_runtime.content_revision('standard', external)
    declaration.write_text(declaration.read_text() + '# changed installed preset\n')
    assert preset['revision'] != preset_runtime.content_revision('standard', external)
    node = command(tmp_path / 'node')
    acpx = command(tmp_path / 'acpx.js')
    with patch.object(dsh_worker, 'NODE', node), \
         patch.object(runtime_paths, 'node_version', return_value='22.13.0'), \
         patch.object(runtime_paths, 'acpx_runtime', return_value=acpx):
        info = dsh_worker.availability()
    assert info['ready'], info['message']
    assert dsh_worker.background_executable(info) == str(external)
    assert info['ui_policy'] == 'background-only'
    with patch.object(runtime_paths, 'node_executable', return_value=node), \
         patch.object(cli_updates.subprocess, 'run') as launch:
        launch.return_value.returncode = 0
        launch.return_value.stdout = '0.2.1-rc.2'
        launch.return_value.stderr = ''
        assert cli_updates._version('dsh', external) == '0.2.1-rc.2'
    assert launch.call_args.args[0] == [str(node), str(external.resolve()), '--version']


def test_catalog_result_in_flight_cannot_repopulate_after_source_change():
    def old_models():
        provider_accounts.invalidate('codex_cli')
        return [{'id': 'old', 'name': 'old'}]
    with patch.object(provider_accounts, '_codex_models', side_effect=old_models):
        result = provider_accounts.catalog('codex_cli')
    assert result['models'] == []
    assert provider_accounts.catalog_snapshot('codex_cli')['state'] == 'unknown'


def test_standalone_dispatch_uses_selected_node_and_npm_entry(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    run = tmp_path / 'run'
    run.mkdir()
    (run / 'prompt.txt').write_text('fixture')
    external = command(tmp_path / 'npm/lib/bin.js')
    node = command(tmp_path / 'separate-node/bin/node')
    monkeypatch.setattr(dsh_worker, 'HERE', tmp_path / 'desk')
    monkeypatch.setattr(dsh_worker, 'NODE', node)
    model = {'id': 'fixture', 'name': 'Fixture', 'provider': 'deepseek', 'model': 'fixture',
             'api_base': 'https://example.invalid/v1', 'max_input_tokens': 32768, 'max_output_tokens': 4096}
    state = {'id': 'fixture', 'model_id': 'fixture', 'kind': 'chat', 'project': str(project),
             'files': [], 'timeout': 30}
    info = {'ready': True, 'executable': str(external), 'command': str(external),
            'ui_policy': 'background-only', 'executor_version': 'standalone:fixture'}
    preset = {'id': 'standard', 'revision': 'fixture', 'source': 'npm', 'overrides': [],
              'policy_overrides': [], 'declaration': {'config': {'plugins': []}}}
    with patch.object(dsh_worker, 'availability', return_value=info), \
         patch.object(dsh_worker.preset_runtime, 'load', return_value=preset), \
         patch.object(dsh_worker, 'subagent_decision', return_value={'decision': 'deny'}), \
         patch('governance_context.fenced_popen', side_effect=OSError('fixture: no launch')):
        with pytest.raises(OSError, match='no launch'):
            dsh_worker._prepare_dsh(run, state, model, SimpleNamespace(url='http://127.0.0.1:9', token='fixture'))
    job = json.loads((run / 'dsh-job.json').read_text())
    assert job['executable'] == str(node)
    assert job['cli'] == str(external.resolve())
    assert job['ui_policy'] == 'background-only'
