"""Bootstrap policy checks with no service, shell or real provider started."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import open_panel as opener
import panel_service as service


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / '打开模型工作台.ps1').write_text('# fixture', encoding='utf-8')
        (self.root / '.local-worker').mkdir()
        (self.root / '.local-worker' / 'start_panel.py').write_text('# fixture', encoding='utf-8')
        self.patch = mock.patch.object(service, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_reuse_running_service_does_not_require_codex_or_start_process(self):
        with mock.patch.object(service, 'workspace', return_value=str(self.root)), \
             mock.patch.object(service.subprocess, 'run') as run:
            result = service.ensure_service('http://127.0.0.1:8765')
        self.assertTrue(result['ready'])
        self.assertFalse(result['started'])
        run.assert_not_called()

    def test_missing_service_uses_hidden_native_opener_with_configured_port(self):
        with mock.patch.object(service, 'workspace', side_effect=[None, str(self.root)]), \
             mock.patch.object(service, 'find_pwsh', return_value='native-pwsh'), \
             mock.patch.object(service.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
            result = service.ensure_service('http://127.0.0.1:9123')
        command = run.call_args.args[0]
        if os.name == 'nt':
            self.assertIn('-NoBrowser', command)
            self.assertEqual(command[-2:], ['-Port', '9123'])
            self.assertIn('creationflags', run.call_args.kwargs)
        else:
            # POSIX opener: start_panel.py detaches the panel server and returns.
            self.assertEqual(command[-3:], [str(self.root / '.local-worker' / 'start_panel.py'), '--port', '9123'])
        self.assertTrue(result['started'])
        self.assertNotIn('shell', run.call_args.kwargs)
        run.assert_called_once()

    def test_foreign_installation_is_not_replaced_or_terminated(self):
        with mock.patch.object(service, 'workspace', return_value=str(self.root / 'other')), \
             mock.patch.object(service.subprocess, 'run') as run, self.assertRaises(ValueError):
            service.ensure_service('http://127.0.0.1:8765')
        run.assert_not_called()

    def test_unconfirmed_start_is_not_retried(self):
        with mock.patch.object(service, 'workspace', return_value=None), \
             mock.patch.object(service, 'find_pwsh', return_value='native-pwsh'), \
             mock.patch.object(service.subprocess, 'run', return_value=mock.Mock(returncode=1)) as run, \
             self.assertRaises(ValueError):
            service.ensure_service('http://127.0.0.1:8765')
        run.assert_called_once()

    def test_invalid_remote_url_fails_before_probe_or_process(self):
        with mock.patch.object(service, 'workspace') as probe, \
             mock.patch.object(service.subprocess, 'run') as run, self.assertRaises(ValueError):
            service.ensure_service('https://example.com')
        probe.assert_not_called()
        run.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'PowerShell 启动器只用于 Windows')
    def test_missing_pwsh_does_not_fall_back_to_another_shell(self):
        with mock.patch.object(service, 'workspace', return_value=None), \
             mock.patch.object(service, 'find_pwsh', side_effect=ValueError('missing native PowerShell')), \
             mock.patch.object(service.subprocess, 'run') as run, self.assertRaises(ValueError):
            service.ensure_service('http://127.0.0.1:8765')
        run.assert_not_called()

    def test_shared_service_keeps_runtime_configuration_but_not_caller_chat_bridge(self):
        with mock.patch.dict(service.os.environ, {
            'MODEL_DESK_PI_HOME': 'fixture-pi', 'MODEL_DESK_DSH_EXE': 'fixture-dsh',
            'MODEL_DESK_COMPLETION_HOST': 'dsh', 'MODEL_DESK_DSH_WAKE_TOKEN': 'fixture-private',
            'CODEX_THREAD_ID': 'fixture-chat', 'CODEX_APP_TOOLS_PIPE_PATH': 'fixture-pipe',
            'FIXTURE_MODEL_API_KEY': 'synthetic-fixture',
        }, clear=True):
            environment = service.startup_environment()
        self.assertEqual(set(environment), {'MODEL_DESK_PI_HOME', 'MODEL_DESK_DSH_EXE', 'FIXTURE_MODEL_API_KEY'})


class MacOpenPanelTests(unittest.TestCase):
    def test_no_browser_reuses_guarded_service_bootstrap(self):
        receipt = {'ready': True, 'started': True, 'url': 'http://127.0.0.1:9123'}
        with mock.patch.object(opener.panel_service, 'ensure_service', return_value=receipt) as ensure, \
             mock.patch.object(opener.webbrowser, 'open') as browser:
            result = opener.open_panel(9123, browser=False)
        ensure.assert_called_once_with('http://127.0.0.1:9123')
        browser.assert_not_called()
        self.assertFalse(result['browser_opened'])

    def test_browser_opens_only_after_same_installation_is_ready(self):
        receipt = {'ready': True, 'started': False, 'url': 'http://127.0.0.1:8765'}
        with mock.patch.object(opener.panel_service, 'ensure_service', return_value=receipt), \
             mock.patch.object(opener.webbrowser, 'open', return_value=True) as browser:
            result = opener.open_panel(8765, browser=True)
        browser.assert_called_once_with('http://127.0.0.1:8765', new=2, autoraise=True)
        self.assertTrue(result['browser_opened'])

    def test_foreign_or_unconfirmed_service_never_opens_browser(self):
        with mock.patch.object(opener.panel_service, 'ensure_service', side_effect=ValueError('foreign')), \
             mock.patch.object(opener.webbrowser, 'open') as browser, \
             self.assertRaisesRegex(ValueError, 'foreign'):
            opener.open_panel(8765, browser=True)
        browser.assert_not_called()

    def test_invalid_port_fails_before_service_probe(self):
        with mock.patch.object(opener.panel_service, 'ensure_service') as ensure, \
             self.assertRaisesRegex(ValueError, '端口'):
            opener.open_panel(0, browser=False)
        ensure.assert_not_called()


if __name__ == '__main__':
    unittest.main()
