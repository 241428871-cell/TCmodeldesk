"""Bounded native CLI planning with explicit permission and durable session identity."""
from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import cli_worker
import control_bootstrap
import goal_brain_workspace
import native_write_sandbox
import platform_runtime
import psutil
from goal_brain_contract import (
    ACTION_SCHEMA,
    BrainError,
    BrainResult,
    Record,
    parse_actions,
    prompt,
)

ROUND_TIMEOUT_SECONDS: Final = 600
MAX_OUTPUT_BYTES: Final = 4 * 1024 * 1024
LOGGER: Final = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    birth: float


def identity(pid: int) -> ProcessIdentity | None:
    try:
        return ProcessIdentity(pid, psutil.Process(pid).create_time())
    except psutil.Error:
        return None


def stop_owned(pid: int, birth: float) -> bool:
    """Only the exact registered native process may be terminated."""
    current = identity(pid)
    if current is None or abs(current.birth - birth) > .01:
        return False
    platform_runtime.stop_tree(pid)
    return True


def inspect_owned(pid: int, birth: float) -> tuple[Literal['running', 'exited', 'unknown'], str]:
    """A missing/reused PID proves the recorded process exited; denied reads do not."""
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - birth) > .01:
            return 'exited', '原 PID 已被其他创建时间的进程复用，未操作该进程'
        if process.status() == psutil.STATUS_ZOMBIE:
            return 'exited', '原 PID 和创建时间匹配，系统确认进程已退出'
        return 'running', '原 PID 和创建时间匹配，原进程仍在运行'
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return 'exited', '系统确认记录中的原进程已不存在'
    except psutil.Error:
        return 'unknown', '系统不允许核对原进程，保留未知状态，禁止重新计算'


def command(engine: str, model: str, project: Path, output: Path, schema: Path,
            runtime: cli_worker.CliRuntime, session_id: str = '',
            permission_mode: str = 'auto') -> tuple[list[str], str]:
    # Bind the bundled source to this installation, without importing a driver or
    # starting any provider during module loading or metadata reads.
    control_bootstrap.bind_source()
    from model_desk_control.native_commands import (
        NativeCommandSpec,
        NativeHost,
        NativeMode,
        build_native_command,
    )
    executable = cli_worker.availability(engine)['executable']
    if not executable:
        raise BrainError('主脑 CLI 尚未安装或未在 PATH 中')
    options = cli_worker.runtime_options(engine, runtime)
    try:
        host, mode = NativeHost(engine), NativeMode(permission_mode)
    except ValueError as error:
        raise BrainError('主脑宿主或权限快照不受支持；没有降级或绕过审批') from error
    launch = build_native_command(NativeCommandSpec(host=host, mode=mode,
        executable=executable, model=model, project=project, output=output, schema=schema,
        schema_json=json.dumps(ACTION_SCHEMA), runtime_args=tuple(options), session_id=session_id))
    return list(launch.arguments), launch.session_id


def _session_from_events(path: Path) -> str:
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get('type') == 'thread.started':
            return str(event.get('thread_id') or '')
    return ''


def _claude_result(path: Path, expected_session: str) -> tuple[str, str]:
    control_bootstrap.bind_source()
    from model_desk_control.claude_output import (
        ClaudeOutputError,
        completion_from_jsonl,
    )
    try:
        value = completion_from_jsonl(path.read_text(encoding='utf-8'), expected_session=expected_session)
    except (ClaudeOutputError, OSError) as error:
        raise BrainError('Claude 主脑结果不可读') from error
    return (json.dumps(value.structured_output, ensure_ascii=False) if value.structured_output is not None
            else value.result, value.session_id)


# The CLI's reported mode must match the requested snapshot; auto is never silently widened.
_CLAUDE_MODES = {'auto': 'auto', 'read_only': 'plan'}


def _claude_effective(path: Path, directory: Path, permission_mode: str) -> None:
    control_bootstrap.bind_source()
    from model_desk_control.claude_output import session_from_jsonl
    effective = session_from_jsonl(path.read_text(encoding='utf-8', errors='replace'))
    if effective is None:
        (directory / 'effective.json').write_text('{"reported": false}', encoding='utf-8')
        return
    (directory / 'effective.json').write_text(effective.model_dump_json(), encoding='utf-8')
    expected = _CLAUDE_MODES.get(permission_mode)
    if effective.permission_mode and effective.permission_mode != expected:
        raise BrainError('Claude 主脑实际权限模式与请求的原生权限不一致，未应用本轮结果')


def run(context: Record, engine: str, model: str, runtime: cli_worker.CliRuntime, directory: Path,
        on_started: Callable[[int, float, str], None], should_stop: Callable[[], bool],
        session_id: str = '', timeout: float = ROUND_TIMEOUT_SECONDS,
        spawn_guard: Callable[[], AbstractContextManager] = nullcontext,
        on_session: Callable[[str], None] | None = None,
        on_exited: Callable[[], None] | None = None,
        permission_mode: str = 'auto') -> BrainResult:
    """No shell interpolation; every generated action still needs host validation."""
    source_project, goal_id = str(context["project"]), str(context["goal_id"])
    workspace = goal_brain_workspace.prepare(source_project, goal_id)
    model_context = goal_brain_workspace.native_context(context, workspace)
    resume_session = (
        session_id
        if goal_brain_workspace.session_is_bound(source_project, goal_id, session_id)
        else ""
    )
    directory.mkdir(parents=True, exist_ok=False)
    schema = directory / 'schema.json'
    output = directory / 'actions.json'
    events = directory / 'native-output.jsonl'
    errors = directory / 'native-errors.txt'
    schema.write_text(json.dumps(ACTION_SCHEMA, ensure_ascii=False), encoding='utf-8')
    (directory / 'context.json').write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding='utf-8')
    args, selected_session = command(engine, model, workspace, output, schema,
                                     runtime, resume_session, permission_mode)
    protected = native_write_sandbox.protect(native_write_sandbox.ProtectRequest(
        engine=engine, argv=tuple(args), workspace=workspace, scratch=directory / 'native-tmp'
    ))
    (directory / 'workspace.json').write_text(
        json.dumps(
            {
                "mode": protected.mode,
                "path": "../native-workspace",
                "legacy_session_reset": bool(session_id and not resume_session),
            }
        ),
        encoding='utf-8',
    )
    process: subprocess.Popen[str] | None = None
    registered: ProcessIdentity | None = None
    try:
        with events.open('w', encoding='utf-8') as stdout, errors.open('w', encoding='utf-8') as stderr:
            with spawn_guard():
                process = subprocess.Popen(protected.argv, cwd=str(workspace), stdin=subprocess.PIPE,
                                           stdout=stdout, stderr=stderr, text=True,
                                           **platform_runtime.process_options())
                registered = identity(process.pid)
                if registered is None:
                    process.kill()
                    raise BrainError('无法核对主脑进程创建时间，未允许自动运行')
                if selected_session:
                    goal_brain_workspace.bind_session(source_project, goal_id, selected_session)
                on_started(registered.pid, registered.birth, selected_session)
            started = time.monotonic()
            first = True
            while True:
                if should_stop():
                    raise BrainError('主脑权限已撤销或用户已暂停/停止')
                if time.monotonic() - started > timeout:
                    raise BrainError('主脑本轮等待超时，需核对原生会话后再续跑')
                if any(p.exists() and p.stat().st_size > MAX_OUTPUT_BYTES for p in (events, errors, output)):
                    raise BrainError('主脑输出超过安全读取上限')
                if engine == 'codex_cli' and not selected_session:
                    selected_session = _session_from_events(events)
                    if selected_session and on_session is not None:
                        goal_brain_workspace.bind_session(source_project, goal_id, selected_session)
                        on_session(selected_session)
                try:
                    process.communicate(
                        input=prompt(model_context, permission_mode) if first else None, timeout=.25
                    )
                    break
                except subprocess.TimeoutExpired:
                    first = False
            if process.returncode != 0:
                raise BrainError('原生主脑退出失败；结果保留在本轮记录中，请核对账户与会话')
        if engine == 'claude_cli':
            raw, selected_session = _claude_result(events, selected_session)
            goal_brain_workspace.bind_session(source_project, goal_id, selected_session)
            _claude_effective(events, directory, permission_mode)
        else:
            selected_session = _session_from_events(events) or resume_session
            if not selected_session:
                raise BrainError('Codex 未返回原生会话标识；停止续跑以避免重复调用')
            if resume_session and selected_session != resume_session:
                raise BrainError('Codex 返回了不同的原生会话，已拒绝替换原会话身份')
            raw = output.read_text(encoding='utf-8')
        actions = parse_actions(raw)
        output.write_text(json.dumps(actions, ensure_ascii=False, indent=2), encoding='utf-8')
        return {'actions': actions, 'native_session_id': selected_session, 'receipt': str(output)}
    finally:
        if process is not None and process.poll() is None and registered is not None:
            stop_owned(registered.pid, registered.birth)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                LOGGER.warning('Native goal worker did not confirm exit; retained clock intent requires reconciliation')
        if process is not None and process.poll() is not None and on_exited is not None:
            on_exited()
