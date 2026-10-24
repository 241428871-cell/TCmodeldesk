"""Stable disposable project snapshots for native decision-model sessions."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import Final, cast
from uuid import uuid4

import model_registry as registry
from goal_brain_contract import BrainError, Record

IDENTITY: Final = re.compile(r"[A-Za-z0-9_-]{1,100}")
EXCLUDED: Final = frozenset(
    {
        ".git",
        ".model-desk",
        ".local-worker",
        ".DS_Store",
        ".omo",
        ".venv",
        ".codex",
        ".claude",
        ".aws",
    }
)


def _exclude(directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in EXCLUDED or (Path(directory) / name).is_symlink()
    }


def _safe_generated(path: Path, parent: Path) -> Path:
    if path.is_symlink() or not path.resolve().is_relative_to(parent.resolve()):
        raise BrainError("主脑隔离工作区路径不可信，未启动原生主脑")
    return path


def prepare(project: str, goal_id: str) -> Path:
    """Refresh one stable Goal path; native resume keeps the same cwd across rounds."""
    if IDENTITY.fullmatch(goal_id) is None:
        raise BrainError("目标标识无效，不能创建主脑隔离工作区")
    source = Path(project)
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise BrainError("主脑项目必须是现有的非链接绝对目录")
    source = source.resolve()
    desk = source / ".model-desk"
    root = desk / "goal-brain" / goal_id
    if desk.is_symlink() or root.is_symlink():
        raise BrainError("主脑记录目录是链接，拒绝创建隔离工作区")
    identity = hashlib.sha256((str(source) + "\0" + goal_id).encode()).hexdigest()
    with registry.file_lock(Path(registry.HERE) / ("brain-workspace-" + identity + ".lock")):
        root.mkdir(parents=True, exist_ok=True)
        target = _safe_generated(root / "native-workspace", root)
        staging = _safe_generated(root / (".native-workspace-" + uuid4().hex + ".tmp"), root)
        previous = _safe_generated(root / (".native-workspace-" + uuid4().hex + ".old"), root)
        try:
            shutil.copytree(source, staging, symlinks=True, ignore=_exclude)
            if target.exists():
                if not target.is_dir():
                    raise BrainError("主脑隔离工作区类型异常，拒绝覆盖")
                os.replace(target, previous)
            os.replace(staging, target)
            if previous.exists():
                shutil.rmtree(previous)
        finally:
            if staging.exists():
                shutil.rmtree(_safe_generated(staging, root))
            if previous.exists() and target.exists():
                shutil.rmtree(_safe_generated(previous, root))
    return target


def _session_marker(project: str, goal_id: str) -> Path:
    if IDENTITY.fullmatch(goal_id) is None:
        raise BrainError("目标标识无效，不能核对主脑会话工作区")
    root = Path(project).resolve() / ".model-desk" / "goal-brain" / goal_id
    return _safe_generated(root / "native-workspace-session.json", root)


def session_is_bound(project: str, goal_id: str, session_id: str) -> bool:
    """Only sessions launched from the stable copy may be resumed after this upgrade."""
    if not session_id:
        return False
    try:
        value = registry.read_json(_session_marker(project, goal_id))
    except (OSError, ValueError):
        return False
    return (
        isinstance(value, dict)
        and value.get("schema") == 1
        and value.get("session_id") == session_id
    )


def bind_session(project: str, goal_id: str, session_id: str) -> None:
    """Bind a provider receipt only after its process was launched in the stable copy."""
    if not session_id or len(session_id) > 512:
        raise BrainError("原生主脑会话标识无效，未保存隔离工作区绑定")
    marker = _session_marker(project, goal_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    registry.write_json(
        marker,
        {"schema": 1, "session_id": session_id, "workspace": "native-workspace"},
    )


def native_context(context: Record, workspace: Path) -> Record:
    """Expose the disposable identity without leaking writable original record paths."""
    copied: Record = dict(context)
    tasks = copied.get("tasks")
    if isinstance(tasks, list):
        copied["tasks"] = [
            {key: value for key, value in task.items() if key != "record_directory"}
            if isinstance(task, dict)
            else task
            for task in tasks
        ]
    source = str(context.get("project") or "").rstrip(os.sep)
    replacement = str(workspace)

    def rebind(value: object) -> object:
        if isinstance(value, str) and source and (
            value == source or value.startswith(source + os.sep)
        ):
            return replacement + value[len(source) :]
        if isinstance(value, list):
            return [rebind(item) for item in value]
        if isinstance(value, dict):
            return {str(key): rebind(item) for key, item in value.items()}
        return value

    rebound = rebind(copied)
    if not isinstance(rebound, dict):
        raise BrainError("主脑上下文无法绑定隔离工作区")
    return cast(Record, rebound)
