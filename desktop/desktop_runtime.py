"""Native desktop adapter; only explicit commands start, stop or move data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import ClassVar, Literal
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

import psutil
from pydantic import BaseModel, ConfigDict


class DesktopError(ValueError):
    """Safe desktop action failure; no subprocess output or credentials."""


class Preferences(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra='forbid')
    format_version: Literal[1] = 1
    data_root: str


class Connection(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra='ignore')
    product: Literal['TCworkdesk'] = 'TCworkdesk'
    protocol: Literal[1] = 1
    workspace: str
    url: str
    pid: int
    birth: float
    managed: bool


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def atomic_json(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as handle:
            handle.write(value.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def default_data_root() -> Path:
    return Path.home() / 'Library/Application Support/TCworkdesk/Data'


def data_root(preferences: Path) -> Path:
    if not preferences.exists():
        return default_data_root()
    try:
        value = Preferences.model_validate_json(preferences.read_bytes())
        path = Path(value.data_root).expanduser()
        if not path.is_absolute():
            raise ValueError('relative path')
        return path
    except (OSError, ValueError) as error:
        raise DesktopError('数据目录设置无法读取；保留原文件，请先修复设置') from error


def template_root() -> Path:
    return Path(sys.executable).parent.parent / 'workbench'


def initialize(template: Path, destination: Path) -> Path:
    """Publish a complete first installation; never overwrite an existing one."""
    install = destination / 'installation'
    if install.exists():
        if not (install / '.local-worker/panel_server.py').is_file():
            raise DesktopError('数据目录中的安装不完整；不会覆盖已有数据')
        return install
    destination.mkdir(parents=True, exist_ok=True)
    staging = destination / ('.installation-' + uuid4().hex)
    try:
        shutil.copytree(template, staging, symlinks=True)
        os.replace(staging, install)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return install


def probe(url: str, expected: Path) -> Connection | None:
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(url + '/api/desktop/identity', timeout=2) as response:
            value = Connection.model_validate_json(response.read(16384))
    except (URLError, OSError, ValueError):
        return None
    if Path(value.workspace).resolve() != expected.resolve() or value.url != url:
        raise DesktopError('端口属于其他工作台，未连接或替换已有服务')
    try:
        if abs(psutil.Process(value.pid).create_time() - value.birth) > .01:
            raise DesktopError('后台进程身份已变化；未连接到过期服务')
    except psutil.Error as error:
        raise DesktopError('后台进程身份无法核实；保留原服务') from error
    return value


def connect(workspace: Path) -> Connection:
    """Serialize cold start with the installation lock and reuse only exact identity."""
    worker = workspace / '.local-worker'
    if not (worker / 'panel_server.py').is_file():
        raise DesktopError('所选目录不是完整的工作台安装')
    sys.path.insert(0, str(worker))
    import model_registry
    with model_registry.file_lock(worker / 'desktop-start.lock'):
        runtime = worker / 'panel-runtime.json'
        if runtime.exists():
            try:
                record = json.loads(runtime.read_bytes())
                url = str(record['url'])
                from urllib.parse import urlparse
                parsed = urlparse(url)
                if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port:
                    raise ValueError('invalid local URL')
                live = probe(url, workspace)
                if live:
                    return live
                if psutil.pid_exists(int(record['pid'])):
                    raise DesktopError('原工作台身份无法核实；保留原服务，请重载后再连接')
            except (KeyError, TypeError, ValueError) as error:
                raise DesktopError('原服务记录无法核实；不会重复启动') from error
        log = worker / 'desktop-service.log'
        environment = {k: v for k, v in os.environ.items() if k not in {
            'CODEX_THREAD_ID', 'CODEX_APP_TOOLS_PIPE_PATH', 'MODEL_DESK_COMPLETION_HOST'}}
        environment['PATH'] = os.pathsep.join([str(worker / 'node-runtime/bin'),
            str(Path.home() / '.local/bin'), '/opt/homebrew/bin', '/usr/local/bin',
            environment.get('PATH', '/usr/bin:/bin')])
        with log.open('ab') as output:
            entry = [] if getattr(sys, 'frozen', False) else [str(Path(__file__).with_name('backend_entry.py'))]
            process = subprocess.Popen([sys.executable, *entry, str(worker / 'panel_server.py'), '--port', '0'],
                cwd=workspace, env=environment, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise DesktopError('后台启动失败；请查看数据目录中的 desktop-service.log')
            if runtime.exists():
                record = json.loads(runtime.read_bytes())
                if record.get('pid') == process.pid:
                    value = probe(str(record['url']), workspace)
                    if value:
                        return value
            time.sleep(.15)
        raise DesktopError('后台启动未确认；保留进程，不自动重试或启动第二份')


def tree_digest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob('*')):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()):
                raise DesktopError('数据含指向目录外的链接，未迁移；请先处理该链接')
            result[name] = 'link:' + os.readlink(path)
        elif path.is_file():
            with path.open('rb') as handle:
                result[name] = hashlib.file_digest(handle, 'sha256').hexdigest()
    return result


def migrate(source: Path, destination: Path, preferences: Path) -> None:
    """Copy, hash-verify, then switch one pointer; keep the source as rollback copy."""
    source, destination = source.resolve(), destination.expanduser().resolve()
    if (source == destination or destination.is_relative_to(source) or source.is_relative_to(destination)):
        raise DesktopError('新旧目录不能相同或互相包含')
    if destination.exists():
        raise DesktopError('请选择尚不存在的目标数据文件夹，避免覆盖已有内容')
    if not source.is_dir():
        raise DesktopError('原数据目录不存在，未更改设置')
    before = tree_digest(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name('.' + destination.name + '.moving-' + uuid4().hex)
    try:
        shutil.copytree(source, staging, symlinks=True)
        if tree_digest(staging) != before or tree_digest(source) != before:
            raise DesktopError('复制校验未通过或原数据仍在变化；未切换目录')
        os.replace(staging, destination)
        atomic_json(preferences, Preferences(data_root=str(destination)))
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def serve(script: Path) -> None:
    """Adapt the existing server; native transport adds no model or clock."""
    sys.path.insert(0, str(script.parent))
    import panel_server as panel
    gate = threading.RLock()
    class DesktopHandler(panel.Handler):
        closing = False

        def do_POST(self) -> None:
            with gate:
                if self.closing:
                    self.send_json({'error': '工作台正准备迁移，请稍后重新打开'}, 503)
                    return
                if self.path == '/api/desktop/shutdown':
                    try:
                        self.guard(mutation=True)
                        if self.headers.get('Content-Length', '0') not in ('0', '2'):
                            raise ValueError('退出操作不接受额外参数')
                        if any(row.get('status') == panel.stage_store.GOAL_ACTIVE for row in panel.stage_store.list_goals()):
                            raise ValueError('仍有启用的目标，请暂停目标后迁移数据')
                        for goal in panel.stage_store.list_goals():
                            if goal.get('project') and panel.continuation_clock.snapshot(goal['project'], goal['id']).get('active_intent'):
                                raise ValueError('原主脑轮次尚未结算，请核对后迁移')
                        panel.cli_source_idle_guard()
                        DesktopHandler.closing = True
                        self.send_json({'ok': True})
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                    except (ValueError, OSError, PermissionError) as error:
                        self.send_json({'error': str(error)}, 409)
                    return
                super().do_POST()

    panel.Handler = DesktopHandler
    panel.DESKTOP_MANAGED = True
    panel.main()


def stop(connection: Connection) -> None:
    if not connection.managed:
        raise DesktopError('当前连接已有工作台；其数据由原安装管理，不自动迁移或停止')
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(connection.url + '/api/session', timeout=3) as response:
        token = str(json.loads(response.read(8192))['token'])
    request = Request(connection.url + '/api/desktop/shutdown', data=b'{}',
        headers={'Content-Type': 'application/json', 'X-Panel-Token': token})
    try:
        with opener.open(request, timeout=5) as response:
            if response.status != 200:
                raise DesktopError('后台未同意迁移，原数据保持原位')
    except URLError as error:
        raise DesktopError('仍有目标、任务或待核对资源；请暂停并处理后迁移') from error
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            process = psutil.Process(connection.pid)
            if abs(process.create_time() - connection.birth) > .01 or process.status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.NoSuchProcess:
            return
        time.sleep(.1)
    raise DesktopError('后台尚未确认退出，未复制或切换数据')


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['connect', 'migrate'])
    parser.add_argument('--preferences', type=Path, required=True)
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args(arguments)
    try:
        root = data_root(args.preferences)
        workspace = args.workspace or initialize(template_root(), root)
        connection = connect(workspace)
        if args.action == 'migrate':
            if args.workspace or args.destination is None:
                raise DesktopError('仅可迁移应用管理的数据目录，且必须指定目标位置')
            stop(connection)
            migrate(root, args.destination, args.preferences)
            try:
                connection = connect(args.destination / 'installation')
            except (OSError, ValueError) as error:
                atomic_json(args.preferences, Preferences(data_root=str(root)))
                raise DesktopError('复制已校验，但新位置后台启动失败；已恢复原路径，目标副本保留') from error
        print(connection.model_dump_json())
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({'error': str(error) if isinstance(error, DesktopError) else '应用操作未完成，原数据保留，请检查目录权限与后台状态'}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
