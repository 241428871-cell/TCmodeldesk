import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_sources as sources


def trees(tmp_path):
    template, installed = tmp_path / 'bundle', tmp_path / 'data/installation'
    for folder, body in ((template, 'new code'), (installed, 'old code')):
        worker = folder / '.local-worker'
        worker.mkdir(parents=True)
        (worker / 'panel_server.py').write_text(body)
    sources.stamp(template)
    return template, installed


def test_upgrade_preserves_configuration_records_and_original_code(tmp_path):
    template, installed = trees(tmp_path)
    models = installed / '.local-worker/models.json'
    models.write_text('{"secret":"fixture-reference"}')
    marker = installed / '.local-worker/private-record.log'
    marker.write_text('unchanged')
    before = models.read_bytes(), marker.read_bytes()
    idle = Mock()
    assert sources.synchronize(template, installed, idle)
    idle.assert_called_once_with()
    assert (installed / '.local-worker/panel_server.py').read_text() == 'new code'
    assert (models.read_bytes(), marker.read_bytes()) == before
    assert [p.read_text() for p in (installed.parent / 'source-backups').rglob('panel_server.py')] == ['old code']
    idle.reset_mock()
    assert not sources.synchronize(template, installed, idle)
    idle.assert_not_called()


def test_busy_backend_prevents_all_replacements(tmp_path):
    template, installed = trees(tmp_path)
    def busy():
        raise sources.SourceUpdateError('busy')
    with pytest.raises(sources.SourceUpdateError, match='busy'):
        sources.synchronize(template, installed, busy)
    assert (installed / '.local-worker/panel_server.py').read_text() == 'old code'
    assert not (installed.parent / 'source-backups').exists()


def test_invalid_bundle_checksum_and_runtime_paths_are_refused(tmp_path):
    template, installed = trees(tmp_path)
    (template / '.local-worker/panel_server.py').write_text('tampered')
    with pytest.raises(sources.SourceUpdateError, match='校验失败'):
        sources.synchronize(template, installed, Mock())
    marker = template / '.desktop-code.json'
    data = json.loads(marker.read_bytes())
    data['files']['.local-worker/models.json'] = 'fixture'
    marker.write_text(json.dumps(data))
    with pytest.raises(sources.SourceUpdateError, match='清单无效'):
        sources.synchronize(template, installed, Mock())


def test_partial_upgrade_is_repaired_on_next_attempt(tmp_path, monkeypatch):
    template, installed = trees(tmp_path)
    for folder in (template, installed):
        (folder / '.local-worker/second.py').write_text('new' if folder == template else 'old')
    sources.stamp(template)
    original = sources.os.replace
    attempts = []
    def interrupted(source, target):
        attempts.append(target)
        if len(attempts) == 2:
            raise OSError('simulated process interruption')
        original(source, target)
    with monkeypatch.context() as change:
        change.setattr(sources.os, 'replace', interrupted)
        with pytest.raises(OSError):
            sources.synchronize(template, installed, Mock())
    assert sources.synchronize(template, installed, Mock())
    assert (installed / '.local-worker/second.py').read_text() == 'new'


def test_external_link_is_never_replaced(tmp_path):
    template, installed = trees(tmp_path)
    outside = tmp_path / 'outside.py'
    outside.write_text('keep')
    target = installed / '.local-worker/panel_server.py'
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(sources.SourceUpdateError, match='外部链接'):
        sources.synchronize(template, installed, Mock())
    assert outside.read_text() == 'keep'
