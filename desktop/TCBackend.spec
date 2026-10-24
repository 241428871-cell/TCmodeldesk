"""Freeze dependencies; actual workbench modules stay beside their writable records."""
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent
worker = root / '.local-worker'
local_modules = {path.stem for path in worker.glob('*.py')}
control_modules = ['model_desk_control.' + path.stem for path in (root / 'control-plane/src/model_desk_control').glob('*.py') if path.stem != '__init__']
datas, binaries, hiddenimports = [], [], sorted(local_modules) + control_modules
for package in ('langgraph', 'langchain_core', 'mcp', 'pydantic', 'keyring', 'tiktoken', 'tiktoken_ext'):
    package_data, package_binaries, package_imports = collect_all(package,
        filter_submodules=lambda name: not name.startswith('mcp.cli') and '.tests' not in name)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_imports
a = Analysis([str(root / 'desktop/backend_entry.py')], pathex=[str(root), str(worker), str(root / 'desktop'), str(root / 'control-plane/src')],
    binaries=binaries, datas=datas, hiddenimports=hiddenimports, hookspath=[], runtime_hooks=[],
    excludes=['pytest', 'IPython', 'tkinter'], noarchive=False)
# These modules must load from the chosen installation, so __file__, resource
# boundaries and the existing control_bootstrap identity check remain correct.
a.pure = [entry for entry in a.pure if entry[0] not in local_modules and not entry[0].startswith('model_desk_control')]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='TCBackend', debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=True, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None)
collect = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='TCBackend')
