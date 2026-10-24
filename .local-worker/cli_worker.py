"""Run installed Claude/Codex CLI accounts in a disposable project copy."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Final, TypedDict
from uuid import uuid4

import cli_launch
import cli_paths
import control_bootstrap
import delegation_policy
import execution_endpoints
import execution_leases
import goal_file_scope
import governance_context
import model_registry as registry
import native_write_sandbox
import platform_runtime
import record_protection
import task_timing
import workflow_settings
from continuation_store import RequirementsChangedError

MAX_CAPTURE_BYTES: Final = 4 * 1024 * 1024

EXECUTABLES = {'claude_cli': 'claude', 'codex_cli': 'codex'}


class CliRuntimeError(ValueError):
    """A native CLI setting or task execution contract could not be satisfied."""


class Availability(TypedDict):
    ready: bool
    name: str
    engine: str
    executable: str
    transport: str
    message: str


class CliRuntime(TypedDict, total=False):
    effort: str
    fast: bool
    context_window: int | None
    autocompact_tokens: int | None


def availability(engine: str) -> Availability:
    if engine not in EXECUTABLES:
        raise CliRuntimeError('未知 CLI 执行端')
    name = EXECUTABLES[engine]
    executable = cli_paths.executable(engine)
    missing = ('所选 Codex 执行端不可用；请在“执行端与 CLI”核对桌面内置／独立 CLI 的选择及安装路径'
               if engine == 'codex_cli' else f'未在 PATH 找到 {name}；请安装并登录该 CLI')
    return {'ready': bool(executable), 'name': 'Claude CLI' if engine == 'claude_cli' else 'Codex CLI',
            'engine': engine, 'executable': str(executable) if executable else '', 'transport': 'local-cli',
            'message': '' if executable else missing}


_HELP_CACHE: dict[tuple[str, int, int], frozenset[str]] = {}
_HELP_ARGS: Final = {'claude_cli': ('--help',), 'codex_cli': ('exec', '--help')}


def installed_flags(engine: str) -> frozenset[str] | None:
    """Options the installed CLI version advertises; None when its help is unreadable.

    Reads ``--help`` only (no prompt, account or model call) and caches the result by
    executable path, size and mtime so an upgrade is detected without a timer.
    """
    executable = availability(engine)['executable'] if engine in EXECUTABLES else ''
    try:
        info = os.stat(executable) if executable else None
    except OSError:
        info = None
    if info is None:
        return None
    key = (executable, info.st_size, info.st_mtime_ns)
    if key not in _HELP_CACHE:
        try:
            done = subprocess.run([executable, *_HELP_ARGS[engine]], capture_output=True, text=True,
                                  encoding='utf-8', errors='replace', timeout=15, check=False,
                                  stdin=subprocess.DEVNULL, **platform_runtime.process_options())
        except (OSError, subprocess.TimeoutExpired):
            return None
        text = done.stdout + '\n' + done.stderr
        _HELP_CACHE[key] = frozenset(word.rstrip(',') for word in text.split() if word.startswith('--'))
    return _HELP_CACHE[key]


def _require_flag(engine: str, flag: str, label: str) -> None:
    flags = installed_flags(engine)
    if flags is not None and flag not in flags:
        raise CliRuntimeError(f'已安装的 CLI 版本不支持 {label}（缺少 {flag}）；请升级 CLI 或改回默认')


def runtime_options(engine: str, runtime: CliRuntime | None = None) -> list[str]:
    """Map a task's immutable project runtime snapshot to native CLI options."""
    runtime = runtime or {}
    effort = runtime.get('effort', 'default')
    levels = {'default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'}
    if effort not in levels:
        raise CliRuntimeError('CLI effort 等级不正确')
    compact = runtime.get('autocompact_tokens')
    context = runtime.get('context_window')
    for value in (compact, context):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1024 or value > 2000000):
            raise CliRuntimeError('CLI 上下文和自动压缩窗口需要 1024–2000000 的整数 tokens')
    if compact is not None and context is not None and compact > context:
        raise CliRuntimeError('自动压缩窗口不能超过上下文窗口')
    if not isinstance(runtime.get('fast', False), bool):
        raise CliRuntimeError('Codex fast 设置需要为布尔值')
    options: list[str] = []
    if engine == 'claude_cli':
        if effort in {'none', 'minimal', 'ultra'}:
            raise CliRuntimeError('Claude CLI 不支持此 effort；请选择 low/medium/high/xhigh/max 或默认')
        if effort != 'default':
            _require_flag(engine, '--effort', 'effort')
            options += ['--effort', effort]
        if compact is not None:
            if not 100000 <= compact <= 1000000:
                raise CliRuntimeError('Claude 自动压缩窗口支持 100000–1000000 tokens')
            _require_flag(engine, '--autocompact', '自动压缩窗口')
            options += ['--autocompact', str(compact)]
        if runtime.get('fast'):
            raise CliRuntimeError('此工作台的 fast 开关仅适用于 Codex CLI')
    else:
        if effort != 'default':
            options += ['-c', 'model_reasoning_effort=' + json.dumps(effort)]
        # Documented tiers are "fast" or one the active model advertises; there is no
        # documented "default" value, so fast off leaves the tier to the model/preset.
        if runtime.get('fast'):
            options += ['-c', 'service_tier="fast"']
        if compact is not None:
            options += ['-c', 'model_auto_compact_token_limit=' + str(compact)]
        if context is not None:
            options += ['-c', 'model_context_window=' + str(context)]
    return options


def command(engine: str, model: str, project: Path, result: Path, kind: str,
            runtime: CliRuntime | None = None) -> list[str]:
    control_bootstrap.bind_source()
    from model_desk_control.claude_output import STREAM_ARGUMENTS
    from model_desk_control.native_commands import (
        NativeMode,
        codex_permission_arguments,
    )
    executable = availability(engine)['executable']
    if not executable:
        raise CliRuntimeError(availability(engine)['message'])
    if engine == 'claude_cli':
        mode = 'plan' if kind == 'chat' else 'auto'
        return [executable, '-p', '--model', model, *STREAM_ARGUMENTS,
                '--permission-mode', mode, '--permission-prompts', 'none',
                *runtime_options(engine, runtime)]
    sandbox = 'read-only' if kind == 'chat' else 'workspace-write'
    permissions = codex_permission_arguments(NativeMode.READ_ONLY if kind == 'chat' else NativeMode.AUTO)
    return [executable, 'exec', *runtime_options(engine, runtime),
            *permissions,
            '--sandbox', sandbox, '--cd', str(project), '--model', model,
            '--skip-git-repo-check', '--output-last-message', str(result), '-']


def _exclude(directory: str, names: list[str]) -> set[str]:
    # Never make control-plane records, Git metadata or nested worker copies
    # available to the executor. Other project tools remain its native choice.
    return {name for name in names if name in {'.git', '.model-desk', '.local-worker', '.DS_Store',
                                              '.omo', '.venv', '.codex', '.claude', '.aws'}
            or (Path(directory) / name).is_symlink()}


def _allowed_path(root: Path, name: str) -> Path:
    target = root / name
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise CliRuntimeError('CLI 修改范围包含链接或项目外路径')
    return target


def record_review(run: Path, state: dict | None, completed: bool, test: dict | None,
                  changed: list[str]) -> None:
    """Report executor outcome without granting acceptance authority.

    Manual and legacy tasks have no delegation marker, so they remain outside
    the independent-review protocol.  Bookkeeping failures are retained on the
    task but must not erase the native executor's durable result.
    """
    if not isinstance(state, dict) or not isinstance(state.get('delegation'), dict):
        return
    try:
        delegation_policy.record_executor(
            run, {'completed': completed, 'test': test, 'changed_files': changed}
        )
    # Review bookkeeping must never break an already durable execution record.
    except Exception as exc:  # noqa: BLE001
        registry.patch_task(run, review_error=str(exc)[:500])


def run_task(run: Path, state: dict, model: dict) -> None:
    execution_leases.validate_task(state, model)
    control_bootstrap.bind_source()
    import cli_writeback
    from model_desk_control import file_writeback
    if state.get('writeback_protocol') is not None or state.get('writeback_capture_id'):
        raise CliRuntimeError('原任务已有写回协议记录，不能重发模型请求；请核对原写回回执')
    engine = state.get('engine')
    if engine not in EXECUTABLES or model.get('provider') != engine:
        raise CliRuntimeError('CLI 执行端与模型账户不匹配')
    settings = workflow_settings.snapshot()
    execution_endpoints.gate(engine, model, settings,
                             automatic=state.get('route', {}).get('mode') == 'auto')
    if (not settings.get('paid_allowed') or settings.get('enforce_paid_budget', True)
            or settings.get('enforce_model_call_limit', True)):
        raise CliRuntimeError('CLI 账户计量未知；执行前必须保持付费授权开启且金额、请求次数上限关闭')
    original = Path(state['project']).resolve() if state.get('project') else None
    if original is None and state['kind'] != 'chat':
        raise CliRuntimeError('CLI 编辑任务需要已有项目绝对目录')
    if original is not None and (not Path(state['project']).is_absolute() or not original.is_dir()):
        raise CliRuntimeError('CLI 项目需要已有绝对目录')
    names = state.get('files') or []
    if state['kind'] == 'edit':
        record_protection.validate_files(names, original)
        names = goal_file_scope.validate_files(str(original), 'selected', names, names)
    base = (run / 'prompt.txt').read_text(encoding='utf-8')
    baseline = file_writeback.snapshot(original, tuple(names)) if state['kind'] == 'edit' else ()
    with tempfile.TemporaryDirectory(prefix='model-desk-cli-') as temporary:
        project = Path(temporary) / 'project'
        if original is None:
            project.mkdir()
        else:
            shutil.copytree(original, project, symlinks=True, ignore=_exclude)
        if state['kind'] == 'edit':
            file_writeback.require_baseline(project, baseline)
            file_writeback.require_baseline(original, baseline)
        for name in names:
            _allowed_path(project, name)
            if original is not None:
                _allowed_path(original, name)
        instructions = ('\n\n你在临时项目副本中工作。可使用本 CLI 可用的原生工具。'
                        '只修改以下指定文件；工作台仅会复制这些文件回原项目：'
                        + json.dumps(names, ensure_ascii=False)
                        + '\n不得读取或修改 .model-desk 记录目录。'
                        + ('这是只读咨询，不要修改任何文件。' if state['kind'] == 'chat' else ''))
        result = Path(temporary) / 'last-message.txt'
        runtime = state.get('cli_runtime', model.get('cli_runtime', {}))
        args = command(engine, model['model'], project, result, state['kind'], runtime)
        capture_id = uuid4().hex
        capture_directory = run / 'native-cli' / capture_id
        capture_directory.mkdir(parents=True, exist_ok=False)
        protected = native_write_sandbox.protect(native_write_sandbox.ProtectRequest(
            engine=engine, argv=tuple(args), workspace=project,
            scratch=capture_directory / 'native-tmp'
        ))
        deadline = task_timing.deadline(state.get('timeout', 0))
        registry.patch_task(run, status='running', phase='运行 ' + engine,
                            started_at=registry.now(), worker_pid=os.getpid(),
                            sandbox_mode=protected.mode,
                            billing_source='cli-account', cost_unknown_calls=1,
                            record_protection=True, cli_runtime=runtime,
                            process_pid=None, process_birth=None, native_output_capture=0, native_capture_id='',
                            cli_permission_mode='read_only' if state['kind'] == 'chat' else 'auto')
        if state['kind'] == 'edit':
            registry.patch_task(run, writeback_protocol=1, writeback_capture_id='',
                                writeback_state='awaiting_native_result')
        stdout_path, stderr_path = capture_directory / 'native-output.txt', capture_directory / 'native-errors.txt'
        with stdout_path.open('x', encoding='utf-8') as captured_out, stderr_path.open('x', encoding='utf-8') as captured_error:
            launch = cli_launch.NativeLaunch(protected.argv, project, captured_out, captured_error, capture_id)
            with cli_launch.spawn(run, state, launch) as process:
                pending_input = base + instructions
                while True:
                    if (run / 'cancelled.flag').exists():
                        platform_runtime.stop_tree(process.pid)
                        process.communicate()
                        registry.patch_task(run, status='cancelled', phase='已停止执行', ended_at=registry.now())
                        return
                    if deadline is not None and time.monotonic() >= deadline:
                        raise CliRuntimeError('CLI 任务超时')
                    if any(path.stat().st_size > MAX_CAPTURE_BYTES for path in (stdout_path, stderr_path)):
                        message = 'CLI 输出超过本轮 4 MiB 上限；保留原始输出，未复制项目改动'
                        raise CliRuntimeError(message)
                    try:
                        with cli_launch.fence(state):
                            process.communicate(pending_input, timeout=.5)
                        break
                    except subprocess.TimeoutExpired:
                        pending_input = None
        with stdout_path.open('r', encoding='utf-8', errors='replace') as captured:
            stdout = captured.read(MAX_CAPTURE_BYTES + 1)
        if len(stdout.encode('utf-8')) > MAX_CAPTURE_BYTES:
            message = 'CLI 输出超过本轮读取上限；保留输出，未复制项目改动'
            raise CliRuntimeError(message)
        if (run / 'cancelled.flag').exists():
            registry.patch_task(run, status='cancelled', phase='已停止执行', ended_at=registry.now())
            return
        if process.returncode:
            raise CliRuntimeError('CLI 执行失败（退出码 %d）；请检查本机 CLI 登录和权限设置' % process.returncode)
        if engine == 'claude_cli':
            control_bootstrap.bind_source()
            from model_desk_control.claude_output import completion_from_jsonl
            completion = completion_from_jsonl(stdout)
            answer = completion.result
            registry.patch_task(run, native_session_id=completion.session_id)
        else:
            answer = result.read_text(encoding='utf-8') if result.is_file() else stdout
        test = None
        if state['kind'] == 'edit' and state.get('test_command'):
            test_command = (['powershell.exe', '-NoProfile', '-Command', state['test_command']]
                            if os.name == 'nt' else ['/bin/sh', '-c', state['test_command']])
            try:
                checked = subprocess.run(test_command, cwd=project, capture_output=True,
                                         text=True, timeout=task_timing.remaining(deadline), check=False)
            except subprocess.TimeoutExpired:
                candidates = capture_directory / 'proposed-files'
                plan = file_writeback.preserve(project, candidates, baseline)
                cli_writeback.prepare(capture_directory, state, plan, answer=answer, test=None)
                raise CliRuntimeError('CLI 修改后的测试超时') from None
            test = {'exit_code': checked.returncode,
                    'output': (checked.stdout + checked.stderr)[-6000:]}
            if checked.returncode:
                registry.patch_task(run, test=test)
                candidates = capture_directory / 'proposed-files'
                plan = file_writeback.preserve(project, candidates, baseline)
                cli_writeback.prepare(capture_directory, state, plan, answer=answer, test=test)
                raise CliRuntimeError('CLI 修改后的测试失败；未写回原项目')
        changed = []
        if state['kind'] == 'edit':
            candidates = capture_directory / 'proposed-files'
            plan = file_writeback.preserve(project, candidates, baseline)
            cli_writeback.prepare(capture_directory, state, plan, answer=answer, test=test)

            def record_applied(applied: tuple[str, ...]) -> None:
                registry.patch_task(run, changed_selected_files=list(applied))

            try:
                _ = goal_file_scope.validate_files(str(original), 'selected', names, names)
                with governance_context.launch_authority(state):
                    if (run / 'cancelled.flag').exists():
                        raise CliRuntimeError('任务已取消，保留提案但未写回')
                    changed = list(file_writeback.apply(plan, original, candidates, record_applied))
            except file_writeback.WritebackConflictError as error:
                registry.patch_task(run, writeback_state='conflict', writeback_conflicts=list(error.names))
                raise
            except (OSError, ValueError):
                registry.patch_task(run, writeback_state='held')
                raise
            registry.patch_task(run, writeback_state='applied')
        completed = registry.patch_task(
            run, status='completed', phase='执行完成，待主脑独立验收',
            ended_at=registry.now(), answer=answer[-20000:],
            changed_selected_files=changed, exit_code=0, test=test,
            cost_note='CLI 账户人民币费用未知；请在账户侧核对'
        )
        record_review(run, completed, True, test, changed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    run = registry.task_dir(args.run_id)
    state = registry.read_task(run)
    try:
        model = registry.get_model(state['model_id'])
        run_task(run, state, model)
        return 0
    except Exception as error:  # noqa: BROAD_EXCEPT_OK — CLI process boundary records task failure.
        if not (run / 'cancelled.flag').exists():
            current = registry.read_task(run)
            pending = bool(current.get('writeback_capture_id')) and current.get('writeback_state') in {
                'prepared', 'held', 'conflict', 'applied'}
            superseded = isinstance(error, RequirementsChangedError)
            fields = {
                'status': 'failed' if superseded else 'recovery_pending' if pending else 'failed',
                'phase': ('运行中需求已更新，旧结果保留未写回' if superseded else
                          '写回待恢复（不会重发模型）' if pending else 'CLI 执行失败'),
                'ended_at': registry.now(), 'error': str(error)[-1500:],
            }
            if superseded:
                fields['writeback_state'] = 'superseded'
                fields['recovery'] = {
                    'kind': 'requirements_superseded', 'reason': str(error)[-1500:],
                    'auto_resend': False,
                }
            elif pending:
                fields['recovery'] = {'kind': 'native_writeback', 'reason': str(error)[-1500:],
                                      'auto_resend': False}
            updated = registry.patch_task(run, **fields)
            record_review(
                run,
                updated,
                False,
                updated.get('test') if isinstance(updated.get('test'), dict) else None,
                list(updated.get('changed_selected_files') or []),
            )
            if superseded and updated.get('stage_id'):
                import goal_events
                import stage_store

                goal_events.sync_task(updated)
                _ = stage_store.mark_requirements_superseded(
                    str(updated['stage_id']),
                    str(updated['id']),
                    str(error)[-1500:],
                    actor='cli-worker',
                    project=str(updated.get('project') or ''),
                )
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
