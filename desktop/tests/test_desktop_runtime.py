"""No account/provider work; data moves use disposable directories only."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_runtime as runtime


def test_missing_preferences_is_read_only_and_defaults_to_user_system_volume(tmp_path):
    path = tmp_path / 'preferences.json'
    result = runtime.data_root(path)
    assert result == Path.home() / 'Library/Application Support/TCmodeldesk/Data'
    assert not path.exists()


def test_corrupt_preferences_never_silently_starts_an_empty_workspace(tmp_path):
    path = tmp_path / 'preferences.json'
    path.write_text('{bad')
    with pytest.raises(runtime.DesktopError):
        runtime.data_root(path)
    assert path.read_text() == '{bad'


def test_initialization_keeps_existing_configuration(tmp_path):
    template = tmp_path / 'template'
    worker = template / '.local-worker'
    worker.mkdir(parents=True)
    (worker / 'panel_server.py').write_text('# bundled source')
    root = tmp_path / 'data'
    install = runtime.initialize(template, root)
    model = install / '.local-worker/models.json'
    model.write_text('{"fixture":true}')
    assert runtime.initialize(template, root) == install
    assert model.read_text() == '{"fixture":true}'


def test_verified_move_preserves_original_and_switches_only_preference_pointer(tmp_path):
    source = tmp_path / 'old'
    source.mkdir()
    (source / 'models.json').write_text('{"keychain":"fixture-reference"}')
    (source / 'cache').mkdir()
    (source / 'cache/value').write_bytes(b'data')
    (source / 'alias').symlink_to('cache/value')
    before = runtime.tree_digest(source)
    destination = tmp_path / 'new disk' / 'TCmodeldesk Data'
    preferences = tmp_path / 'preferences.json'
    runtime.migrate(source, destination, preferences)
    assert runtime.tree_digest(source) == before == runtime.tree_digest(destination)
    assert runtime.data_root(preferences) == destination
    assert (destination / 'alias').is_symlink()


@pytest.mark.parametrize('relationship', ['same', 'child', 'parent', 'existing', 'alias'])
def test_unsafe_move_targets_do_not_overwrite(tmp_path, relationship):
    source = tmp_path / 'old'
    source.mkdir()
    (source / 'data').write_text('keep')
    target = {'same': source, 'child': source / 'child', 'parent': tmp_path,
              'existing': tmp_path / 'existing', 'alias': tmp_path / 'alias'}[relationship]
    if relationship == 'existing':
        target.mkdir()
    if relationship == 'alias':
        target.symlink_to(source, target_is_directory=True)
    preferences = tmp_path / 'preferences.json'
    with pytest.raises(runtime.DesktopError):
        runtime.migrate(source, target, preferences)
    assert not preferences.exists()
    assert (source / 'data').read_text() == 'keep'


def test_external_links_are_not_copied_into_managed_data(tmp_path):
    source = tmp_path / 'old'
    source.mkdir()
    (source / 'external').symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(runtime.DesktopError, match='链接'):
        runtime.migrate(source, tmp_path / 'new', tmp_path / 'preferences.json')


def test_source_changes_during_copy_prevent_pointer_switch(tmp_path):
    source = tmp_path / 'old'
    source.mkdir()
    (source / 'data').write_text('before')
    original = runtime.shutil.copytree
    def moving(*args, **kwargs):
        result = original(*args, **kwargs)
        (source / 'data').write_text('concurrent write')
        return result
    with patch.object(runtime.shutil, 'copytree', side_effect=moving):
        with pytest.raises(runtime.DesktopError, match='变化'):
            runtime.migrate(source, tmp_path / 'new', tmp_path / 'preferences.json')
    assert not (tmp_path / 'new').exists()
    assert not (tmp_path / 'preferences.json').exists()


def test_existing_installation_cannot_be_stopped_as_owned_backend():
    value = runtime.Connection(workspace='/fixture', url='http://127.0.0.1:1234', pid=1, birth=1, managed=False)
    with patch.object(runtime, 'build_opener') as network:
        with pytest.raises(runtime.DesktopError, match='原安装'):
            runtime.stop(value)
    network.assert_not_called()


def test_rename_reads_legacy_pointer_without_moving_or_overwriting(tmp_path):
    support = tmp_path / 'Library/Application Support'
    legacy = support / 'TCworkdesk/desktop.json'
    legacy.parent.mkdir(parents=True)
    old_root = tmp_path / 'existing data'
    legacy.write_text(json.dumps({'data_root': str(old_root)}))
    preferences = support / 'TCmodeldesk/desktop.json'
    with patch.object(runtime.Path, 'home', return_value=tmp_path):
        assert runtime.data_root(preferences) == old_root
    assert not preferences.exists()
    assert json.loads(legacy.read_text())['data_root'] == str(old_root)


def test_rename_finds_legacy_installation_without_preferences(tmp_path):
    support = tmp_path / 'Library/Application Support'
    worker = support / 'TCworkdesk/Data/installation/.local-worker'
    worker.mkdir(parents=True)
    (worker / 'panel_server.py').write_text('# legacy')
    with patch.object(runtime.Path, 'home', return_value=tmp_path):
        assert runtime.data_root(support / 'TCmodeldesk/desktop.json') == support / 'TCworkdesk/Data'


def test_quit_never_restarts_a_backend_that_already_exited():
    connection = runtime.Connection(workspace='/fixture', url='http://127.0.0.1:1234', pid=999999, birth=1, managed=True)
    with patch.object(runtime.psutil, 'Process', side_effect=runtime.psutil.NoSuchProcess(999999)):
        with patch.object(runtime, 'connect') as connect, patch.object(runtime, 'build_opener') as network:
            assert runtime.main(['stop', '--preferences', '/unused', '--connection', connection.model_dump_json()]) == 0
    connect.assert_not_called()
    network.assert_not_called()


def test_quit_refuses_a_replaced_service_before_loading_session():
    connection = runtime.Connection(workspace='/fixture', url='http://127.0.0.1:1234', pid=1, birth=1, managed=True)
    replacement = connection.model_copy(update={'pid': 2, 'birth': 2})
    with patch.object(runtime.psutil, 'Process') as process:
        process.return_value.create_time.return_value = 1
        process.return_value.status.return_value = 'running'
        with patch.object(runtime, 'probe', return_value=replacement):
            with patch.object(runtime, 'build_opener') as network:
                with pytest.raises(runtime.DesktopError, match='身份'):
                    runtime.stop(connection)
    network.assert_not_called()
