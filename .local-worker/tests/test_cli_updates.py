from pathlib import Path
from datetime import datetime, timedelta, timezone
import sys
from unittest.mock import MagicMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_updates
import panel_server


@pytest.fixture(autouse=True)
def clear_update_cache():
    cli_updates._CACHE.clear()
    yield
    cli_updates._CACHE.clear()


def test_bundled_cli_is_detected_but_application_is_not_rewritten():
    executable = Path('/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh')
    with patch.object(cli_updates, '_executable', return_value=executable), \
         patch.object(cli_updates, '_version', return_value='0.2.1-rc.2'):
        row = cli_updates.status('dsh')
        assert row['source'] == 'host-app'
        assert row['can_update'] is False
        with pytest.raises(ValueError, match='应用包'):
            cli_updates.update('dsh')


def test_symlink_to_bundled_cli_keeps_host_managed_identity(tmp_path):
    app = tmp_path / 'DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh'
    app.parent.mkdir(parents=True)
    app.write_text('fixture')
    link = tmp_path / 'bin/dsh'
    link.parent.mkdir()
    link.symlink_to(app)
    with patch.object(cli_updates, '_executable', return_value=link), \
         patch.object(cli_updates, '_version', return_value='0.2.1-rc.2'):
        assert cli_updates.status('dsh')['source'] == 'host-app'


def test_claude_native_update_uses_its_official_updater():
    executable = Path('/fixture/claude')
    completed = Mock(returncode=0, stdout='updated', stderr='')
    with patch.object(cli_updates, '_executable', return_value=executable), \
         patch.object(cli_updates, '_version', side_effect=['2.1.0', '2.2.0']), \
         patch.object(cli_updates.subprocess, 'run', return_value=completed) as run:
        row = cli_updates.update('claude_cli')
    assert row['installed'] == '2.2.0'
    run.assert_called_once()
    assert run.call_args.args[0] == ['/fixture/claude', 'update']


def test_recognized_codex_standalone_uses_native_update_without_app_rewrite(tmp_path):
    executable = tmp_path / 'release/bin/codex'
    executable.parent.mkdir(parents=True)
    executable.write_text('fixture')
    (executable.parent.parent / 'codex-package.json').write_text(
        '{"layoutVersion":1,"version":"0.164.0","variant":"codex","entrypoint":"bin/codex"}')
    completed = Mock(returncode=0, stdout='', stderr='')
    with patch.object(cli_updates, '_executable', return_value=executable), \
         patch.object(cli_updates, '_version', return_value='0.164.0'), \
         patch.object(cli_updates.subprocess, 'run', return_value=completed) as run:
        row = cli_updates.update('codex_cli')
    assert row['source'] == 'standalone'
    assert row['method'] == 'codex update'
    run.assert_called_once()
    assert run.call_args.args[0] == [str(executable), 'update']


def test_update_check_remains_visible_in_subsequent_status():
    executable = Path('/fixture/claude')
    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.read.return_value = b'{"version":"2.2.0"}'
    opener = Mock()
    opener.open.return_value = response
    with patch.object(cli_updates, '_executable', return_value=executable), \
         patch.object(cli_updates, '_version', return_value='2.1.0'), \
         patch.object(cli_updates, 'build_opener', return_value=opener):
        checked = cli_updates.check('claude_cli')
        current = cli_updates.status('claude_cli')
    assert checked['update_available'] is True
    assert current['checked'] is True
    assert current['latest'] == '2.2.0'
    assert current['update_available'] is True
    assert current['checked_at']
    assert current['version_source'] == 'https://registry.npmjs.org/@anthropic-ai/claude-code/latest'


def test_failed_refresh_keeps_old_version_explicitly_stale():
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b'{"version":"2.2.0"}'
    opener = Mock()
    opener.open.return_value = response
    with patch.object(cli_updates, '_executable', return_value=Path('/fixture/claude')), \
         patch.object(cli_updates, '_version', return_value='2.1.0'), \
         patch.object(cli_updates, 'build_opener', return_value=opener):
        before = cli_updates.check('claude_cli')
        opener.open.side_effect = TimeoutError('offline')
        with pytest.raises(ValueError, match='无法读取'):
            cli_updates.check('claude_cli')
        after = cli_updates.status('claude_cli')
    assert after['latest'] == '2.2.0'
    assert after['checked_at'] == before['checked_at']
    assert after['version_status'] == 'unavailable'
    assert after['stale'] is True
    assert after['update_available'] is None


def test_expired_version_does_not_keep_update_offer():
    with patch.object(cli_updates, '_executable', return_value=Path('/fixture/claude')), \
         patch.object(cli_updates, '_version', return_value='2.1.0'):
        receipt = cli_updates.status('claude_cli')
        receipt.update(checked=True, latest='2.2.0', update_available=True, version_status='ready',
                       checked_at=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat())
        cli_updates._CACHE['claude_cli'] = receipt
        current = cli_updates.status('claude_cli')
    assert current['version_status'] == 'stale'
    assert current['update_available'] is None


def test_failed_version_command_cannot_report_a_version_from_error_text(tmp_path):
    executable = tmp_path / 'codex'
    executable.write_text('fixture')
    done = Mock(returncode=1, stdout='', stderr='requires version 22.13.0')
    with patch.object(cli_updates.subprocess, 'run', return_value=done):
        assert cli_updates._version('codex_cli', executable) == ''


def test_missing_cli_is_not_the_working_directory_or_an_updatable_install():
    with patch.object(cli_updates.cli_paths.shutil, 'which', return_value=None):
        row = cli_updates.status('claude_cli')
    assert row['executable'] == ''
    assert row['installed'] == ''
    assert row['can_update'] is False


def test_failed_signature_verification_is_not_certified_from_metadata(tmp_path):
    executable = tmp_path / 'dsh'
    executable.write_text('fixture')
    failed = Mock(returncode=1)
    detail = Mock(returncode=0, stderr='Authority=Developer ID Application: Fixture\nTeamIdentifier=FIXTURE')
    with patch.object(cli_updates.sys, 'platform', 'darwin'), \
         patch.object(cli_updates.subprocess, 'run', side_effect=[failed, detail]):
        receipt = cli_updates._signature(executable)
    assert receipt['status'] == 'unavailable'
    assert receipt['checked_at']


def test_cli_update_activity_includes_native_brain_rounds():
    with patch.object(panel_server, 'execution_activity',
                      return_value={'tasks': [], 'complete': True}), \
         patch.object(panel_server.stage_store, 'list_goals', return_value=[{
             'id': 'goal_1', 'project': '/fixture/project',
             'status': panel_server.stage_store.GOAL_ACTIVE,
         }]), \
         patch.object(panel_server.continuation_clock, 'snapshot', return_value={
             'active_intent': {'id': 'round_1'}, 'brain_model_id': 'claude-model',
         }):
        activity = panel_server.cli_update_activity()
    assert activity['complete'] is True
    assert activity['tasks'] == []
    assert activity['goals'] == [{
        'goal_id': 'goal_1', 'project': '/fixture/project',
        'intent_id': 'round_1', 'brain_model_id': 'claude-model',
    }]


def test_cli_update_activity_fails_closed_when_goal_state_is_unreadable():
    with patch.object(panel_server, 'execution_activity',
                      return_value={'tasks': [], 'complete': True}), \
         patch.object(panel_server.stage_store, 'list_goals', side_effect=OSError('unreadable')):
        activity = panel_server.cli_update_activity()
    assert activity == {'tasks': [], 'goals': [], 'complete': False}

    with patch.object(panel_server, 'execution_activity',
                      return_value={'tasks': [], 'complete': True}), \
         patch.object(panel_server.stage_store, 'list_goals', return_value=[{
             'id': 'goal_1', 'project': '/fixture/project',
             'status': panel_server.stage_store.GOAL_ACTIVE,
         }]), \
         patch.object(panel_server.continuation_clock, 'snapshot', return_value={
             'active_intent': 'malformed',
         }):
        activity = panel_server.cli_update_activity()
    assert activity == {'tasks': [], 'goals': [], 'complete': False}
