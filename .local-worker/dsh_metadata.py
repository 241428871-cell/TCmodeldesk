"""Explicit, bounded DSH catalog/account probe without agents or inference."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Final

import platform_runtime
import runtime_paths
from native_metadata import JsonMap, ProviderError

MARKER: Final = 'MODEL_DESK_METADATA:'
BRIDGE: Final = Path(__file__).parent / 'agent-runtime/dsh-metadata.cjs'


def probe(*, balance: bool = False, command: Path | None = None,
          overlays: Sequence[JsonMap] = ()) -> JsonMap:
    """Mount a metadata-only bridge in a disposable official ACP profile."""
    selected = command or runtime_paths.dsh_command()
    if selected is None:
        raise ProviderError('所选 DSH CLI 未安装；请检查桌面内置／独立 CLI 来源')
    args = [str(selected)]
    from cli_updates import _version
    version = _version('dsh', selected)
    if not version:
        raise ProviderError('无法读取所选 DSH CLI 版本，请检查安装')
    if '.app/Contents/' not in str(selected.resolve()):
        from cli_sources import dsh_package
        try:
            _, package = dsh_package(selected)
        except ValueError:
            raise ProviderError('无法核对独立 DSH 官方软件包') from None
        node = runtime_paths.node_executable()
        if node is None:
            raise ProviderError('DSH CLI 需要 Node 22 运行时')
        args = [str(node), str(selected.resolve())]
        version = package.version
    with tempfile.TemporaryDirectory(prefix='tcmodeldesk-dsh-metadata-') as temporary:
        directory = Path(temporary)
        patch = directory / 'metadata.json'
        patch.write_text(json.dumps([
            *[{'id': name, 'disabled': True} for name in ('acp', 'acp-app-startup',
               'session-title-llm', 'session-log-deepseek', 'session-telemetry-otel')],
            {'id': 'credentials', 'config': {'dshHome': os.environ.get('DSH_HOME') or str(Path.home() / '.dsh'),
                                           'watch': False}},
            *overlays,
            {'insert': [{'id': 'model-desk-account-controller', 'name': '@deepseek-ai/dsh-api-account-controller'},
                        {'id': 'model-desk-metadata', 'name': str(BRIDGE.resolve()),
                         'config': {'balance': balance, 'version': version}}]},
        ]), encoding='utf-8')
        env = {**os.environ, 'DSH_HOME': str(directory / 'home'),
               'DSH_TELEMETRY_MODE': 'DISABLED'}
        args += ['--profile', 'acp', '--patch', str(patch)]
        try:
            with subprocess.Popen(args, cwd=directory, env=env, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                    encoding='utf-8', errors='replace', **platform_runtime.process_options()) as child:
                lines: Queue[str | None] = Queue(maxsize=128)
                def read() -> None:
                    if child.stdout is not None:
                        for _ in range(128):
                            line = child.stdout.readline(1024 * 1024)
                            if not line:
                                break
                            lines.put(line)
                    lines.put(None)
                Thread(target=read, daemon=True).start()
                try:
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        try:
                            line = lines.get(timeout=max(.01, deadline - time.monotonic()))
                        except Empty:
                            break
                        if line is None:
                            break
                        if line.startswith(MARKER):
                            value: JsonMap = json.loads(line[len(MARKER):])
                            if isinstance(value, dict) and isinstance(value.get('models'), list):
                                return value
                            raise ProviderError('DSH 返回的模型目录格式不正确')
                finally:
                    if child.poll() is None:
                        platform_runtime.stop_tree(child.pid)
                    child.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            if isinstance(error, ProviderError):
                raise
            raise ProviderError('DSH 元数据通道不可用，请检查 CLI 安装与网络') from None
    raise ProviderError('DSH 未返回模型目录；未创建会话或发送生成请求')
