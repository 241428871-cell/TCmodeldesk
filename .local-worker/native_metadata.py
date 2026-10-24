"""Bounded, short-lived native metadata transport; never sends model prompts."""
from __future__ import annotations

import json
from queue import Empty, Queue
import subprocess
import tempfile
from threading import Thread
import time
from typing import Final

import platform_runtime
import cli_paths

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonMap = dict[str, JsonValue]


class ProviderError(ValueError):
    """A safe, non-secret provider metadata error for the panel boundary."""


_TIMEOUT: Final = 20.0
_MAX_LINE: Final = 2 * 1024 * 1024


def _map(value: JsonValue) -> JsonMap:
    return value if isinstance(value, dict) else {}


class NativeMetadata:
    """Own one short-lived native metadata-only process and its bounded reader."""

    def __init__(self, provider: str):
        self.provider = provider
        self.process: subprocess.Popen[str] | None = None
        self.directory: tempfile.TemporaryDirectory[str] | None = None
        self.lines: Queue[str | None] = Queue()
        self.sequence = 0

    def __enter__(self) -> NativeMetadata:
        executable = cli_paths.executable(self.provider)
        if not executable:
            raise ProviderError('未找到本机 CLI，请先安装并将它加入 PATH')
        self.directory = tempfile.TemporaryDirectory(prefix='model-desk-metadata-')
        args = [str(executable), 'app-server', '--listen', 'stdio://']
        if self.provider == 'claude_cli':
            args = [str(executable), '-p', '--input-format', 'stream-json',
                    '--output-format', 'stream-json', '--verbose',
                    '--no-session-persistence', '--strict-mcp-config',
                    '--mcp-config', '{"mcpServers":{}}',
                    '--settings', '{"disableAllHooks":true}',
                    '--permission-mode', 'plan', '--permission-prompts', 'none']
        try:
            self.process = subprocess.Popen(args, cwd=self.directory.name,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace',
                **platform_runtime.process_options())
        except OSError:
            self.directory.cleanup()
            raise ProviderError('本机 CLI 无法启动，请检查安装与执行权限') from None
        Thread(target=self._read, daemon=True).start()
        try:
            if self.provider == 'codex_cli':
                self.call('initialize', {'clientInfo': {'name': 'model_desk', 'version': '1.0'}})
                self._send({'method': 'initialized', 'params': {}})
            else:
                self.initial = self.call('initialize', {'hooks': {}, 'sdkMcpServers': []})
        except (ProviderError, OSError):
            self.close()
            raise
        return self

    def _read(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            self.lines.put(None)
            return
        try:
            for _ in range(1024):
                line = process.stdout.readline(_MAX_LINE + 1)
                if not line or len(line) > _MAX_LINE:
                    break
                self.lines.put(line)
        except (OSError, ValueError):
            self.lines.put(None)
            return
        self.lines.put(None)

    def _send(self, message: JsonMap) -> None:
        if self.process is None or self.process.stdin is None:
            raise ProviderError('CLI 元数据通道尚未就绪')
        try:
            self.process.stdin.write(json.dumps(message) + '\n')
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise ProviderError('CLI 元数据通道已关闭，请升级 CLI 后重试') from None

    def call(self, method: str, params: JsonMap) -> JsonMap:
        """Send only a fixed metadata request; never send a prompt or user turn."""
        self.sequence += 1
        request_id = str(self.sequence)
        if self.provider == 'claude_cli':
            self._send({'type': 'control_request', 'request_id': request_id,
                        'request': {'subtype': method, **params}})
        else:
            self._send({'id': self.sequence, 'method': method, 'params': params})
        deadline = time.monotonic() + _TIMEOUT
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=max(.01, deadline - time.monotonic()))
            except Empty:
                raise ProviderError('CLI 元数据读取超时，请检查登录和网络') from None
            if line is None:
                raise ProviderError('CLI 未返回可读取的元数据；此版本可能不支持该接口')
            try:
                reply = _map(json.loads(line))
            except ValueError:
                continue
            if self.provider == 'claude_cli':
                response = _map(reply.get('response'))
                if response.get('request_id') != request_id:
                    continue
                if response.get('subtype') != 'success':
                    raise ProviderError('Claude 元数据接口不可用，请检查 CLI 版本及账户权限')
                return _map(response.get('response'))
            if reply.get('id') == self.sequence:
                if reply.get('error'):
                    raise ProviderError('Codex 元数据接口不可用，请检查 CLI 登录及账户权限')
                return _map(reply.get('result'))
        raise ProviderError('CLI 元数据读取超时，请稍后重试')

    def close(self) -> None:
        process = self.process
        if process is not None:
            if process.poll() is None:
                platform_runtime.stop_tree(process.pid)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
        if self.directory is not None:
            self.directory.cleanup()

    def __exit__(self, *_args) -> None:
        self.close()
