import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dsh_worker
import model_registry as registry
import preset_runtime as presets


def installed_asar():
    try:
        return presets._asar_path(dsh_worker.EXECUTABLE)
    except ValueError:
        return None


def write_asar(path, relative, payload):
    parts = relative.split('/')
    node = {'size': len(payload), 'offset': '0'}
    for part in reversed(parts):
        node = {'files': {part: node}}
    table = json.dumps(node, separators=(',', ':')).encode()
    padding = b'\0' * (-len(table) % 4)
    header = b''.join(value.to_bytes(4, 'little') for value in (4, len(table) + 8, len(table) + 4, len(table)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + table + padding + payload)
    return len(table)


class PresetTests(unittest.TestCase):
    @unittest.skipUnless(installed_asar(), '需要本机已安装 DSH 的 app.asar；未安装时不伪造')
    def test_named_definitions_and_shared_restrictions(self):
        standard = presets.load({'provider': 'deepseek'}, dsh_worker.EXECUTABLE)
        self.assertEqual(standard['id'], 'standard')
        standard_rows = standard['declaration']['config']['plugins']
        self.assertIn('planning', [r['id'] for r in standard_rows])
        self.assertTrue(next(r for r in standard_rows if r['id'] == 'delegation')['disabled'])
        self.assertTrue(next(r for r in standard_rows if r['id'] == 'tool-web')['disabled'])
        self.assertIsInstance(next(r for r in standard_rows if r['id'] == 'tool-pwsh')['disabled'], presets.JS)
        if presets.NATIVE.is_file():
            native = presets.load({'provider': 'local'}, dsh_worker.EXECUTABLE)
            self.assertEqual(native['id'], 'native')
            native_rows = native['declaration']['config']['plugins']
            persona = next(r for r in native_rows if r['id'] == 'persona')
            self.assertTrue(persona['config']['complete'])
            self.assertNotIn('planning', [r['id'] for r in native_rows])
            self.assertTrue(next(r for r in native_rows if r['id'] == 'tool-web')['disabled'])

    def test_revision_or_preset_change_requires_new_session(self):
        model = {'id': 'home', 'api_base': 'http://localhost/v1', 'model': 'qwen', 'max_input_tokens': 262144, 'max_output_tokens': 8192}
        a = dsh_worker.fingerprint(model, 'edit', 'project', {'id': 'native', 'revision': 'a'})
        b = dsh_worker.fingerprint(model, 'edit', 'project', {'id': 'standard', 'revision': 'a'})
        c = dsh_worker.fingerprint(model, 'edit', 'project', {'id': 'native', 'revision': 'b'})
        self.assertEqual(len({a, b, c}), 3)

    def test_runtime_proof_required_and_must_match(self):
        preset = {'id': 'native', 'revision': 'a'}
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            with self.assertRaises(ValueError):
                presets.verify(run, preset)
            registry.write_json(run / 'preset-proof.json', {'preset': 'standard', 'revision': 'a', 'session_id': 'session'})
            with self.assertRaises(ValueError):
                presets.verify(run, preset)
            registry.write_json(run / 'preset-proof.json', {'preset': 'native', 'revision': 'a', 'session_id': 'session'})
            self.assertEqual(presets.verify(run, preset, 'session')['preset'], 'native')
            with self.assertRaises(ValueError):
                presets.verify(run, preset, 'other-session')

    def test_asar_reader_skips_four_byte_header_padding(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / 'app.asar'
            payload = b'{"version":"0.2.1-rc.2"}'
            table_size = write_asar(archive, 'dsh/package.json', payload)
            self.assertNotEqual(table_size % 4, 0)
            self.assertEqual(presets._asar_read(archive, 'dsh/package.json'), payload)

    def test_asar_path_accepts_macos_bundle_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / 'DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness'
            archive = executable.parent.parent / 'Resources/app.asar'
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b'executable')
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b'archive')
            self.assertEqual(presets._asar_path(executable), archive)


if __name__ == '__main__':
    unittest.main(verbosity=2)
