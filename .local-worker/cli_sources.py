"""Filesystem-only backend selection, shared by tasks, brains and metadata."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

Source = Literal['desktop', 'standalone']
Endpoint = Literal['codex_cli', 'dsh']


class SourceOption(TypedDict):
    id: Source
    label: str
    available: bool
    executable: str


class BackendSources(TypedDict):
    selected: Source | None
    options: list[SourceOption]


class Package(BaseModel):
    model_config = ConfigDict(frozen=True, extra='ignore')
    name: str
    version: str
    bin: dict[str, str] = Field(default_factory=dict)


def selected_source(endpoint: Endpoint) -> Source | None:
    # Lazy import avoids coupling installation discovery to server startup.
    import execution_endpoints
    state = execution_endpoints.snapshot()
    if not state['readable']:
        return None
    row = state['endpoints'].get(endpoint)
    if not isinstance(row, dict):
        return None
    value = row.get('cli_source', 'desktop')
    return value if value in ('desktop', 'standalone') else None


def standalone_dsh() -> Path | None:
    declared = os.environ.get('MODEL_DESK_DSH_CLI', '').strip()
    if declared:
        candidates = [Path(declared).expanduser()]
    else:
        found = shutil.which('dsh')
        candidates = ([Path(found)] if found else []) + [Path.home() / '.local/bin/dsh']
        candidates += [Path(directory) / 'dsh' for directory in os.get_exec_path()]
    return next((p for p in candidates if p.is_absolute() and p.is_file()
                 and os.access(p, os.X_OK) and '.app/Contents/' not in str(p.resolve())), None)


def dsh_package(command: Path) -> tuple[Path, Package]:
    """Bind package identity to the selected npm executable, never the desktop."""
    target = command.resolve()
    for directory in target.parents:
        manifest = directory / 'package.json'
        if not manifest.is_file():
            continue
        try:
            package = Package.model_validate_json(manifest.read_bytes())
        except (OSError, ValueError):
            continue
        if package.name == '@deepseek-ai/dsh' and package.bin.get('dsh'):
            if (directory / package.bin['dsh']).resolve() == target:
                return directory, package
    raise ValueError('独立 DSH CLI 的官方 npm 包及入口无法核对')


def dsh_web_package(command: Path) -> Path:
    root, _ = dsh_package(command)
    for directory in (root, *root.parents):
        candidate = directory / 'node_modules/@deepseek-ai/dsh-web-app'
        try:
            manifest = json.loads((candidate / 'package.json').read_bytes())
            if manifest.get('name') == '@deepseek-ai/dsh-web-app':
                return candidate
        except (OSError, ValueError, AttributeError):
            continue
    raise ValueError('独立 DSH 缺少同一安装中的官方 web-app 预设包')


def backend_sources(endpoint: Endpoint) -> BackendSources:
    import cli_paths
    import runtime_paths
    desktop = cli_paths.desktop_codex() if endpoint == 'codex_cli' else runtime_paths.dsh_desktop_command()
    standalone = cli_paths.standalone_codex() if endpoint == 'codex_cli' else standalone_dsh()
    return {'selected': selected_source(endpoint), 'options': [
        {'id': 'desktop', 'label': '桌面版内置（默认）', 'available': desktop is not None,
         'executable': str(desktop or '')},
        {'id': 'standalone', 'label': '独立 CLI', 'available': standalone is not None,
         'executable': str(standalone or '')}]}
