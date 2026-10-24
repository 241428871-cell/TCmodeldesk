"""Source-aware, explicit update checks for installed CLI endpoints."""
from __future__ import annotations

import re
import subprocess
import threading
import sys
from datetime import datetime, timezone
from contextlib import closing
from http.client import HTTPResponse
from pathlib import Path
from typing import ClassVar, Literal, NotRequired, Protocol, TypedDict
from urllib.error import URLError
from urllib.request import Request, build_opener, ProxyHandler

from pydantic import BaseModel, ConfigDict

import runtime_paths
import cli_paths

IDS = ('dsh', 'claude_cli', 'codex_cli')
PACKAGES = {'dsh': '@deepseek-ai/dsh', 'claude_cli': '@anthropic-ai/claude-code',
            'codex_cli': '@openai/codex'}
class Signature(TypedDict):
    status: Literal['unchecked', 'verified', 'unavailable']
    authority: str
    team_id: str
    checked_at: str


class CliStatus(TypedDict):
    engine: str
    installed: str
    executable: str
    source: str
    method: str
    can_update: bool
    checked: bool
    latest: str
    update_available: bool | None
    note: str
    version_status: Literal['unchecked', 'ready', 'stale', 'unavailable']
    checked_at: str
    attempted_at: str
    version_source: str
    stale: bool
    error: str
    signature: Signature
    updated: NotRequired[bool]


class VersionPackage(BaseModel):
    version: str


class CodexPackage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)
    layoutVersion: Literal[1]
    version: str
    variant: Literal['codex']
    entrypoint: Literal['bin/codex']


def _standalone_package(executable: Path | None, installed: str) -> bool:
    if executable is None:
        return False
    try:
        resolved = executable.resolve(strict=True)
        metadata = resolved.parent.parent / 'codex-package.json'
        package = CodexPackage.model_validate_json(metadata.read_bytes())
        return package.version == installed and resolved == (metadata.parent / package.entrypoint).resolve()
    except (OSError, ValueError):
        return False


class VersionOpener(Protocol):
    def open(self, fullurl: Request, *, timeout: float) -> HTTPResponse: ...


def _package_opener() -> VersionOpener:
    return build_opener(ProxyHandler())


_CACHE: dict[str, CliStatus] = {}
_LOCK = threading.Lock()
MAX_VERSION_AGE_SECONDS = 86400


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signature(executable: Path | None) -> Signature:
    receipt: Signature = {'status': 'unavailable', 'authority': '', 'team_id': '', 'checked_at': _now()}
    if sys.platform != 'darwin' or not executable or not executable.is_file():
        return receipt
    resolved = executable.resolve()
    target = next((p for p in resolved.parents if p.suffix == '.app'), resolved)
    try:
        verified = subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(target)],
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=False)
        detail = subprocess.run(['/usr/bin/codesign', '-dv', '--verbose=4', str(target)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return receipt
    # A failed system trust check is not proof of tampering (sandboxed hosts can
    # lack access to trust services). Never label it verified from the path alone.
    receipt['status'] = 'verified' if verified.returncode == 0 else 'unavailable'
    for line in detail.stderr.splitlines():
        if line.startswith('Authority=') and not receipt['authority']:
            receipt['authority'] = line.partition('=')[2]
        if line.startswith('TeamIdentifier='):
            receipt['team_id'] = line.partition('=')[2]
    return receipt


def _executable(engine: str) -> Path | None:
    if engine == 'dsh':
        return runtime_paths.dsh_command()
    return cli_paths.executable(engine)


def _version(_engine: str, executable: Path | None) -> str:
    if not executable or not executable.is_file():
        return ''
    args = [str(executable), '--version']
    if _engine == 'dsh' and '.app/Contents/' not in str(executable.resolve()):
        from cli_sources import dsh_package
        try:
            _ = dsh_package(executable)
            node = runtime_paths.node_executable()
            if node is None:
                return ''
            args = [str(node), str(executable.resolve()), '--version']
        except ValueError:
            pass  # A non-npm launcher owns its own interpreter.
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=15, check=False,
                                start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        return ''
    if result.returncode:
        return ''
    match = re.search(r'\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?', result.stdout + '\n' + result.stderr)
    return match.group(0) if match else ''


def status(engine: str) -> CliStatus:
    if engine not in IDS:
        raise ValueError('此执行端不是可更新的 CLI')
    executable = _executable(engine)
    installed = _version(engine, executable)
    path = str(executable) if executable else ''
    try:
        source_path = str(executable.resolve(strict=True)) if executable else ''
    except OSError:
        source_path = path
    bundled = '.app/Contents/' in source_path
    import dsh_installation
    if engine == 'codex_cli' and path and not bundled and _standalone_package(executable, installed):
        method, can_update = 'codex update', True
        note = '已识别 Codex 独立安装包，不依赖桌面应用；只在点击更新后调用其原生 codex update。'
    elif engine == 'dsh' and dsh_installation.update_command(executable):
        method, can_update = 'official-npm', True
        note = '独立官方 DSH 安装；显式更新仅修改此 npm 前缀，不改桌面应用、账户或插件配置。'
    elif engine == 'claude_cli' and path:
        method, can_update = 'claude update', True
        note = '使用 Claude 官方原生更新器；只在点击更新后执行。'
    elif bundled:
        method, can_update = 'host-app', False
        host = 'DeepSeek Harness' if engine == 'dsh' else 'Codex／ChatGPT 桌面'
        note = f'此 CLI 归属 {host} 应用包并随应用更新，工作台不会改写应用包。'
    else:
        method, can_update = 'external', False
        note = ('独立 Codex CLI，不依赖桌面应用；请使用 OpenAI 官方独立安装器更新。'
                if engine == 'codex_cli' and path else '安装来源无法安全确认；请用原安装器更新。')
    row: CliStatus = {'engine': engine, 'installed': installed, 'executable': path,
           'source': 'host-app' if bundled else 'standalone' if engine in ('codex_cli', 'dsh') and path else 'native', 'method': method,
           'can_update': can_update, 'checked': False, 'latest': '',
           'update_available': None, 'note': note,
           'version_status': 'unchecked', 'checked_at': '', 'attempted_at': '',
           'version_source': f'https://registry.npmjs.org/{PACKAGES[engine]}/latest',
           'stale': False, 'error': '', 'signature': {'status': 'unchecked', 'authority': '',
                                                    'team_id': '', 'checked_at': ''}}
    with _LOCK:
        cached = _CACHE.get(engine)
    if cached and (cached.get('installed'), cached.get('executable')) == (installed, path):
        row = {**cached, 'note': note}
        if row['checked_at']:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row['checked_at'])).total_seconds()
            row['stale'] = row['stale'] or age > MAX_VERSION_AGE_SECONDS
            if row['stale'] and row['version_status'] == 'ready':
                row['version_status'] = 'stale'
        if row['stale']:
            row['update_available'] = None
    signature = row['signature']
    row['note'] += (f" 签名已核验：{signature['authority']}（{signature['checked_at']}）。" if signature['status'] == 'verified'
                    else ' 签名尚未核验或当前环境无法核验。')
    if row['attempted_at']:
        row['note'] += f" 官方版本来源 {row['version_source']}；最近尝试 {row['attempted_at']}。"
    return row


def check(engine: str) -> CliStatus:
    row = status(engine)
    row['signature'] = _signature(_executable(engine))
    row['attempted_at'] = _now()
    package = PACKAGES[engine].replace('/', '%2f')
    request = Request(f'https://registry.npmjs.org/{package}/latest',
                      headers={'Accept': 'application/json', 'User-Agent': 'Model-Desk/1'})
    try:
        opener = _package_opener()
        with closing(opener.open(request, timeout=15)) as response:
            latest = VersionPackage.model_validate_json(response.read(65536)).version
        if not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?', latest):
            raise ValueError('软件包目录没有返回有效版本')
    except (URLError, OSError, TimeoutError, ValueError) as error:
        row.update(version_status='unavailable', stale=bool(row['checked']),
                   update_available=None, error='官方版本读取失败')
        with _LOCK:
            _CACHE[engine] = row.copy()
        raise ValueError('无法读取官方软件包版本：' + str(error)[:180]) from None
    comparable = row['can_update']
    row.update({'checked': True, 'latest': latest,
                'checked_at': _now(), 'version_status': 'ready', 'stale': False, 'error': '',
                'update_available': newer(latest, row['installed']) if comparable and latest and row['installed'] else None})
    with _LOCK:
        _CACHE[engine] = row.copy()
    return status(engine)


def update(engine: str) -> CliStatus:
    row = status(engine)
    if not row['can_update']:
        raise ValueError(row['note'])
    executable = row['executable']
    command = [executable, 'update']
    if engine == 'dsh':
        import dsh_installation
        managed = dsh_installation.update_command(Path(executable))
        if managed is None or not newer(check(engine)['latest'], row['installed']):
            raise ValueError('没有已确认的新版本，或独立安装归属已变化；未更新')
        command = list(managed)
    result = subprocess.run(command, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=300, check=False, start_new_session=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-800:]
        raise ValueError('CLI 更新失败：' + (detail or f'退出码 {result.returncode}'))
    with _LOCK:
        _CACHE.pop(engine, None)
    after = status(engine)
    after['updated'] = True
    return after


def newer(latest: str, installed: str) -> bool | None:
    """Do not offer a downgrade from a newer release candidate to npm latest."""
    def version(value: str) -> tuple[int, int, int, int, str, int] | None:
        match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-([a-zA-Z]+)[.-]?(\d+)?)?(?:\+.*)?', value)
        if match is None:
            return None
        major, minor, patch, tag, number = match.groups()
        return int(major), int(minor), int(patch), int(tag is None), tag or '', int(number or 0)
    first, second = version(latest), version(installed)
    return first > second if first is not None and second is not None else None
