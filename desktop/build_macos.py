"""Build a native local macOS app without accounts, records or host CLI binaries."""
from __future__ import annotations

import argparse
import importlib.metadata
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def copy_notices(resources: Path) -> None:
    licenses = resources / 'licenses'
    licenses.mkdir(exist_ok=True)
    shutil.copy2(ROOT / '.local-worker/node-runtime/LICENSE', licenses / 'Node-LICENSE.txt')
    python_licenses = [Path(sys.base_prefix) / 'LICENSE_PYTHON.txt',
                       Path(sys.base_prefix) / 'lib/python3.12/LICENSE.txt']
    python_license = next((path for path in python_licenses if path.is_file()), None)
    if python_license is None:
        raise RuntimeError('Python distribution license is missing')
    shutil.copy2(python_license, licenses / 'Python-LICENSE.txt')
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get('Name', 'package')
        target = licenses / (name + '-' + distribution.version)
        for index, relative in enumerate(distribution.files or []):
            if any(part.lower() in {'license', 'licenses', 'license.txt', 'copying', 'copying.txt'} for part in relative.parts):
                source = Path(str(distribution.locate_file(relative)))
                if source.is_file():
                    target.mkdir(exist_ok=True)
                    shutil.copy2(source, target / (str(index) + '-' + source.name))


def run(arguments: list[str], timeout: int = 300) -> None:
    subprocess.run(arguments, cwd=ROOT, check=True, timeout=timeout)


def copy_source(destination: Path) -> None:
    source = ROOT / '.local-worker'
    worker = destination / '.local-worker'
    worker.mkdir(parents=True)
    for path in source.glob('*.py'):
        shutil.copy2(path, worker / path.name)
    for name in ('acceptance_profiles.json', 'aider.yml', 'empty.env'):
        shutil.copy2(source / name, worker / name)
    for name in ('panel', 'workflow-guides'):
        shutil.copytree(source / name, worker / name, ignore=shutil.ignore_patterns('__pycache__'))
    runtime = worker / 'agent-runtime'
    runtime.mkdir()
    for path in (source / 'agent-runtime').iterdir():
        if path.is_file() and path.suffix in {'.json', '.mjs', '.cjs', '.ts', '.yaml'}:
            shutil.copy2(path, runtime / path.name)
    shutil.copytree(source / 'agent-runtime/node_modules', runtime / 'node_modules', symlinks=True)
    node = worker / 'node-runtime/bin'
    node.mkdir(parents=True)
    shutil.copy2(source / 'node-runtime/bin/node', node / 'node')
    shutil.copytree(ROOT / 'control-plane/src', destination / 'control-plane/src',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('LICENSE', 'THIRD_PARTY.md', 'README.md', 'AGENTS.md', 'CLAUDE.md', 'requirements.txt', 'environment.yml'):
        shutil.copy2(ROOT / name, destination / name)
    docs = destination / 'docs'
    docs.mkdir()
    local_only = {'Codex交接.md', 'Claude交接.md', 'Goal任务.md', '前端视觉收尾交接.md'}
    for path in (ROOT / 'docs').glob('*.md'):
        if path.name not in local_only:
            shutil.copy2(path, docs / path.name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/macos')
    parser.add_argument('--reuse-backend', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise SystemExit('macOS 构建仅在 macOS 上运行')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    build = ROOT / 'build/macos'
    build.mkdir(parents=True, exist_ok=True)
    if not args.reuse_backend:
        run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', str(build / 'frozen'),
             '--workpath', str(build / 'pyinstaller'), str(ROOT / 'desktop/TCBackend.spec')], timeout=600)
    app = output / 'TCmodeldesk.app'
    if app.exists():
        raise SystemExit('输出应用已存在；请选择新输出目录，不覆盖已有安装')
    contents = app / 'Contents'
    resources = contents / 'Resources'
    (contents / 'MacOS').mkdir(parents=True)
    resources.mkdir()
    shutil.copytree(build / 'frozen/TCBackend', resources / 'backend', symlinks=True)
    copy_source(resources / 'workbench')
    copy_notices(resources)
    info = {'CFBundleIdentifier': 'local.tcmodeldesk.desktop', 'CFBundleName': 'TCmodeldesk',
        'CFBundleDisplayName': 'TCmodeldesk', 'CFBundleExecutable': 'TCmodeldesk',
        'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': '0.3.2',
        'CFBundleVersion': '3', 'LSMinimumSystemVersion': '13.0', 'NSHighResolutionCapable': True,
        'CFBundleIconFile': 'TCmodeldesk',
        'NSPrincipalClass': 'NSApplication', 'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True}}
    with (contents / 'Info.plist').open('wb') as handle:
        plistlib.dump(info, handle)
    environment = dict(os.environ)
    environment['CLANG_MODULE_CACHE_PATH'] = str(build / 'clang-cache')
    subprocess.run(['/usr/bin/xcrun', 'swiftc', '-parse-as-library', '-O',
        '-target', f'{platform.machine()}-apple-macosx13.0',
        '-module-cache-path', str(build / 'swift-cache'), '-framework', 'AppKit', '-framework', 'WebKit',
        str(ROOT / 'desktop/macos/TCmodeldesk.swift'), '-o', str(contents / 'MacOS/TCmodeldesk')],
        cwd=ROOT, env=environment, timeout=120, check=True)
    subprocess.run(['/usr/bin/xcrun', 'swiftc', '-module-cache-path', str(build / 'swift-cache'),
        str(ROOT / 'desktop/macos/Icon.swift'), '-o', str(build / 'make-icon')],
        cwd=ROOT, env=environment, timeout=60, check=True)
    run([str(build / 'make-icon'), str(build / 'TCmodeldesk.iconset')], timeout=15)
    run(['/usr/bin/iconutil', '-c', 'icns', str(build / 'TCmodeldesk.iconset'),
         '-o', str(resources / 'TCmodeldesk.icns')], timeout=15)
    run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', str(app)], timeout=120)
    run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], timeout=60)
    print(f'已生成本机签名应用：{app}')


if __name__ == '__main__':
    main()
