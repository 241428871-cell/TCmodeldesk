from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict


class SourceUpdateError(ValueError):
    """An incomplete code update must not start a mixed backend."""


class CodeManifest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    format: Literal[1] = 1
    revision: str
    files: dict[str, str]


def _allowed(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name:
        return False
    return ((len(path.parts) == 2 and path.parts[0] == '.local-worker' and path.suffix == '.py')
            or (len(path.parts) == 3 and path.parts[:2] == ('.local-worker', 'panel')
                and path.suffix in ('.js', '.css', '.html', '.svg'))
            or (len(path.parts) == 3 and path.parts[:2] == ('.local-worker', 'agent-runtime')
                and path.suffix in ('.mjs', '.cjs', '.js', '.ts'))
            or (path.parts[:2] == ('control-plane', 'src') and path.suffix == '.py'))


def _digest(path: Path) -> str:
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def stamp(template: Path) -> None:
    files = {path.relative_to(template).as_posix(): _digest(path)
             for path in sorted(template.rglob('*')) if path.is_file()
             and _allowed(path.relative_to(template).as_posix())}
    revision = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    (template / '.desktop-code.json').write_text(
        CodeManifest(revision=revision, files=files).model_dump_json(indent=2), encoding='utf-8')


@contextmanager
def _lock(installation: Path) -> Iterator[None]:
    with (installation / '.local-worker/desktop-start.lock').open('a+b') as handle:
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise SourceUpdateError('另一个应用正在启动或升级工作台，请稍后重试') from None
                time.sleep(.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def synchronize(template: Path, installation: Path, idle_guard: Callable[[], None]) -> bool:
    """Update only shipped code after idle proof; retry repairs an interrupted update."""
    marker = template / '.desktop-code.json'
    if not marker.is_file():
        return False
    manifest = CodeManifest.model_validate_json(marker.read_bytes())
    if not manifest.files or any(not _allowed(name) for name in manifest.files):
        raise SourceUpdateError('应用代码清单无效；未改动账户或项目数据')
    for name, digest in manifest.files.items():
        source = template / name
        if not source.resolve().is_relative_to(template.resolve()) or _digest(source) != digest:
            raise SourceUpdateError('应用内置代码校验失败，请重新下载应用')
    if not (installation / '.local-worker').resolve().is_relative_to(installation.resolve()):
        raise SourceUpdateError('安装代码目录含外部链接，未覆盖现有文件')
    for name in manifest.files:
        if not (installation / name).resolve().is_relative_to(installation.resolve()):
            raise SourceUpdateError('安装代码路径含外部链接，未覆盖现有文件')
    with _lock(installation):
        changed = [name for name, digest in manifest.files.items()
                   if not (installation / name).is_file() or _digest(installation / name) != digest]
        if not changed:
            return False
        for name in changed:
            if not (installation / name).resolve().is_relative_to(installation.resolve()):
                raise SourceUpdateError('安装代码路径含外部链接，未覆盖现有文件')
        idle_guard()
        backup = installation.parent / 'source-backups' / manifest.revision
        if not backup.resolve().is_relative_to(installation.parent.resolve()):
            raise SourceUpdateError('代码备份目录含外部链接，未覆盖现有文件')
        for name in changed:
            target = installation / name
            original = backup / name
            if target.is_file() and not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, original)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.' + uuid4().hex + '.upgrade')
            try:
                shutil.copy2(template / name, temporary)
                with temporary.open('rb') as saved:
                    os.fsync(saved.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        receipt = installation / ('.desktop-code-' + uuid4().hex + '.tmp')
        try:
            shutil.copy2(marker, receipt)
            os.replace(receipt, installation / '.desktop-code.json')
        finally:
            receipt.unlink(missing_ok=True)
    return True
