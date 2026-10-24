"""Explicit native-main-brain lifecycle, governed by one durable goal clock."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import logging
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Final, assert_never
from uuid import uuid4

import continuation_clock
import control_bootstrap
from goal_actions import Panel, apply
from goal_brain_context import build_context, receipt_directory, select_brain
from goal_brain_contract import BrainError, Record, parse_actions
import goal_brain_runner
import goal_recovery
import goal_settlement
import goal_events
from goal_recovery import settle_terminal
import project_storage
import stage_store

if TYPE_CHECKING:
    from model_desk_control.host_settlement import SettlementRequest

LOGGER: Final = logging.getLogger(__name__)
OWNER_ID: Final = 'native-goal-driver-' + uuid4().hex
_LOCK: Final = threading.RLock()
_STOP: Final = threading.Event()
_RUNNING: Final[dict[tuple[str, str], threading.Thread]] = {}
_driver: threading.Thread | None = None
_panel: Panel | None = None


def start_goal(project: str, goal_id: str, body: Record | None = None) -> Record:
    """Validate explicit human intent and return immediately; the driver owns work."""
    project = project_storage.normalize(project)
    values = body or {}
    state = continuation_clock.snapshot(project, goal_id)
    selection = select_brain(project, str(values.get('model_id') or state.get('brain_model_id') or ''))
    if stage_store.get_goal(goal_id, project) is None:
        raise BrainError('目标不存在')
    if not state['exists']:
        continuation_clock.configure_clock(project, goal_id,
            interval_seconds=int(values.get('interval_seconds') or 1800), enabled=False,
            brain_model_id=selection.model_id)
    return control_goal(project, goal_id, str(values.get('action') or 'start'),
                        {'model_id': selection.model_id})


def control_goal(project: str, goal_id: str, action: str, payload: Record | None = None) -> Record:
    """Revoke authority before stopping verified native/worker processes."""
    if action == 'handoff':
        import goal_handoff

        def validate_model() -> None:
            select_brain(project, str((payload or {}).get('model_id') or ''))

        return goal_handoff.handoff(project, goal_id, payload or {}, validate_model)
    if action == 'settle_terminal':
        return settle_terminal(project, goal_id)
    if action in {'inspect', 'reconcile_owned', 'reconcile'}:
        return goal_recovery.inspect_goal(project, goal_id)
    before = continuation_clock.snapshot(project, goal_id)
    if not before['exists'] and action in {'pause', 'stop'}:
        continuation_clock.configure_clock(project, goal_id, enabled=False)
        before = continuation_clock.snapshot(project, goal_id)
    if action in {'start', 'resume', 'run_now'}:
        control_bootstrap.require_graph()
        if 'native_session_id' in (payload or {}):
            raise BrainError('此入口不接受未验证的原生会话标识；系统只续接当前目标已登记的会话')
        model_id = str((payload or {}).get('model_id') or before.get('brain_model_id') or '')
        if before.get('native_session_id') and model_id != before.get('brain_model_id'):
            raise BrainError('此目标已有原生主脑会话；请沿用原决策模型，切换模型时新建目标以保留会话身份')
        selection = select_brain(project, model_id)
        payload = {**(payload or {}), 'model_id': selection.model_id}
    after = continuation_clock.control(project, goal_id, action, payload)
    if action in {'stop', 'pause'}:
        intent = before.get('active_intent') or {}
        if intent.get('pid') and intent.get('birth'):
            goal_brain_runner.stop_owned(int(intent['pid']), float(intent['birth']))
        if action == 'stop' and _panel is not None:
            goal = stage_store.get_goal(goal_id, project)
            for stage in (goal or {}).get('stages', []):
                task_id = stage.get('active_task_id')
                if task_id:
                    try:
                        _panel.cancel_task(str(task_id), reason='用户停止目标', actor='human')
                    except (ValueError, OSError):
                        LOGGER.info('Goal task already ended or cannot be safely stopped: %s', task_id)
    return after


@contextmanager
def brain_launch(project: str, goal_id: str, epoch: int, model_id: str) -> Iterator[None]:
    """Recheck binding, switch, endpoint and paid authorization at the brain's real start."""
    with continuation_clock.authority_guard(project, goal_id, epoch):
        select_brain(project, model_id)
        yield


def run_once(project: str, goal_id: str, panel: Panel, owner_id: str = OWNER_ID) -> Record | None:
    """Claim a due intent or reconcile its saved actions without a new native call."""
    control_bootstrap.require_graph()
    import goal_resume
    from goal_graph_bridge import GoalGraphHost
    from model_desk_control.host_flow import (HostApplied, HostCallbacks, HostNativeResult,
        HostProfile, HostRef, HostSnapshot, HostStep)
    from model_desk_control.native_commands import NativeMode
    project = project_storage.normalize(project)
    claim = continuation_clock.claim_due(project, goal_id, owner_id, lease_seconds=120, host_protocol='graph_v2')
    recovery = goal_resume.candidate(project, goal_id, owner_id) if claim is None else None
    if recovery is not None:
        claim, owner_id = recovery.record, recovery.reference.owner_id
    if claim is None:
        return None
    intent_id = str(claim['intent_id'])
    epoch = int(claim['authority_epoch'])
    started = False
    invoked = False
    native_completed = False
    effects_started = recovery is not None
    native_session = str(claim.get('native_session_id') or '')
    last_renewal = time.monotonic()

    def should_stop() -> bool:
        nonlocal last_renewal
        state = continuation_clock.snapshot(project, goal_id)
        if (_STOP.is_set() or state['authority_epoch'] != epoch or state['user_intent'] != 'RUN'
                or not state['enabled'] or state['hold']):
            return True
        if (not native_completed and int(state.get('requirements_revision', 0))
                > int(claim.get('requirements_revision', 0))):
            return True
        if time.monotonic() - last_renewal > 30:
            continuation_clock.renew_lease(project, goal_id, intent_id, epoch, lease_seconds=120)
            last_renewal = time.monotonic()
        return False

    def on_started(pid: int, birth: float, native_session_id: str) -> None:
        nonlocal started, native_session
        continuation_clock.record_runner(project, goal_id, intent_id, epoch, pid, birth,
                                          native_session_id=native_session_id)
        started = True
        native_session = native_session_id or native_session

    def on_session(session_id: str) -> None:
        nonlocal native_session
        continuation_clock.record_session(project, goal_id, intent_id, epoch, session_id)
        native_session = session_id

    def on_exited() -> None:
        state = continuation_clock.snapshot(project, goal_id)
        if state['authority_epoch'] != epoch:
            continuation_clock.control(project, goal_id, 'reconcile', {'intent_id': intent_id,
                'worker_exited': True, 'evidence': '本服务持有的原生子进程已由 Popen.poll/wait 确认退出'})

    def finish(result: Record, *, settlement: SettlementRequest | None = None) -> Record:
        try:
            return continuation_clock.finish_round(project, goal_id, intent_id, epoch, result, settlement=settlement)
        except continuation_clock.ClockError:
            state = continuation_clock.snapshot(project, goal_id)
            if (state['authority_epoch'] != epoch or state['hold'] or state['user_intent'] != 'RUN'
                    or state.get('active_intent_id') != intent_id):
                return state
            raise

    def snapshot_phase() -> str:
        selection = select_brain(project, str(claim.get('brain_model_id') or ''))
        context = build_context(
            project,
            goal_id,
            selection,
            requirements_through=int(claim.get('requirements_revision', 0)),
        )
        context['trigger'] = goal_events.context(project, goal_id, claim)
        directory = receipt_directory(project, goal_id, intent_id)
        return HostSnapshot(context=context, engine=selection.engine, model=selection.model,
            runtime=HostProfile.model_validate(selection.runtime), directory=str(directory),
            session_id=str(claim.get('native_session_id') or ''),
            permission_mode=NativeMode(str(claim.get('brain_permission_mode') or 'read_only')),
            contract_revision=int(context['contract']['contract_revision']),
            action_receipts_version=2, settle_receipts_version=2,
            plan_revision=int(context['contract']['plan_revision'])).model_dump_json()

    def native_phase() -> str:
        nonlocal invoked, native_completed
        snapshot = HostSnapshot.model_validate_json(host.result(HostStep.SNAPSHOT))
        if should_stop():
            raise BrainError('续跑授权已撤销')
        invoked = True
        runtime = {'effort': snapshot.runtime.effort, 'fast': snapshot.runtime.fast,
                   'context_window': snapshot.runtime.context_window,
                   'autocompact_tokens': snapshot.runtime.autocompact_tokens}
        result = goal_brain_runner.run(snapshot.context, snapshot.engine, snapshot.model, runtime,
                                      Path(snapshot.directory), on_started, should_stop,
                                      session_id=snapshot.session_id,
                                      spawn_guard=lambda: brain_launch(project, goal_id, epoch,
                                                                       str(claim.get('brain_model_id') or '')),
                                      on_session=on_session, on_exited=on_exited,
                                      permission_mode=snapshot.permission_mode.value)
        native_completed = True
        return HostNativeResult(actions_json=json.dumps(result['actions'], ensure_ascii=False),
            native_session_id=result['native_session_id'], receipt=result['receipt']).model_dump_json()

    def actions_phase() -> str:
        nonlocal effects_started
        snapshot = HostSnapshot.model_validate_json(host.result(HostStep.SNAPSHOT))
        result = HostNativeResult.model_validate_json(host.result(HostStep.NATIVE))
        if should_stop():
            raise BrainError('续跑授权已撤销，旧轮次结果未应用')
        effects_started = True
        try:
            with continuation_clock.authority_guard(project, goal_id, epoch):
                outcome = apply(project, goal_id, epoch, parse_actions(result.actions_json), panel,
                                snapshot.context['engineering_policy'], result.receipt,
                                contract_revision=snapshot.contract_revision, scope=host.reference,
                                plan_revision=snapshot.plan_revision,
                                permission_mode=snapshot.permission_mode.value)
        except continuation_clock.RequirementsChangedError as error:
            outcome = {'dispatched_task_ids': [], 'goal_completed': False,
                       'pending_obligation_ids': [], 'summary': str(error), 'superseded': True}
        return HostApplied.model_validate_json(json.dumps(outcome)).model_dump_json()

    def settle_phase() -> str:
        settlement = goal_settlement.from_results(host.reference,
            host.result(HostStep.ACTIONS), host.result(HostStep.NATIVE))
        finish(goal_settlement.payload(settlement), settlement=settlement)
        return settlement.receipt()

    try:
        host = GoalGraphHost(HostRef(project=project, goal_id=goal_id, operation_id=intent_id,
            epoch=epoch, owner_id=owner_id), HostCallbacks(snapshot_phase, native_phase,
                actions_phase, settle_phase), replay_actions=True, replay_settle=True,
                enter=(lambda: goal_resume.adopt(recovery)) if recovery is not None else None)
        host.run()
        return settle_terminal(project, goal_id)
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        state = continuation_clock.snapshot(project, goal_id)
        if (state['authority_epoch'] == epoch and state['user_intent'] == 'RUN'
                and int(state.get('requirements_revision', 0))
                > int(claim.get('requirements_revision', 0))):
            return finish({'status': 'superseded',
                           'summary': ('运行中需求已从 R%s 更新为 R%s；旧主脑进程已停止，下一安全轮次重新规划'
                                       % (claim.get('requirements_revision', 0),
                                          state.get('requirements_revision', 0))),
                           'retryable': True, 'native_session_id': native_session})
        if recovery is None and not invoked and state['authority_epoch'] != epoch and (state.get('active_intent') or {}).get('id') == intent_id:
            return continuation_clock.release_unstarted(project, goal_id, intent_id, epoch, owner_id,
                '持有本轮领取的服务确认：权限已撤销，尚未调用原生启动器，未创建主脑进程')
        status = 'unknown' if started or effects_started else 'blocked'
        return finish({'status': status, 'summary': str(error), 'retryable': False,
                       'native_session_id': native_session})


def _run(project: str, goal_id: str, panel: Panel) -> None:
    try:
        run_once(project, goal_id, panel)
    except (ValueError, OSError, RuntimeError):
        LOGGER.exception('Goal continuation failed; durable intent must be reconciled before retry')
    finally:
        with _LOCK:
            _RUNNING.pop((project, goal_id), None)


def _scan(panel: Panel) -> None:
    control_bootstrap.require_graph()
    from goal_scan import ScanAction, decide

    while not _STOP.is_set():
        try:
            for goal in stage_store.list_goals():
                project = goal.get('project')
                if not project or not Path(project).is_dir():
                    continue
                key = (project, goal['id'])
                with _LOCK:
                    if key in _RUNNING:
                        continue
                if goal['status'] == 'active':
                    panel.resume_queued(project, goal['id'])
                match decide(project, goal['id'], goal['status'], OWNER_ID):
                    case ScanAction.SKIP:
                        continue
                    case ScanAction.SETTLE:
                        settle_terminal(project, goal['id'])
                    case ScanAction.RUN:
                        with _LOCK:
                            if key in _RUNNING or len(_RUNNING) >= 2:
                                continue
                            worker = threading.Thread(target=_run, args=(project, goal['id'], panel), daemon=True,
                                                      name='goal-round-' + goal['id'])
                            _RUNNING[key] = worker
                            worker.start()
                    case unreachable:
                        assert_never(unreachable)
        except (ValueError, OSError, RuntimeError):
            LOGGER.exception('Cannot inspect goal clocks; no new native call was authorized')
        _STOP.wait(2)


def start_driver(panel: Panel) -> None:
    """Called only by the server main after its runtime is explicitly started."""
    global _driver, _panel
    with _LOCK:
        if _driver is not None and _driver.is_alive():
            return
        _panel = panel
        _STOP.clear()
        _driver = threading.Thread(target=_scan, args=(panel,), daemon=True, name='goal-clock-driver')
        _driver.start()


def stop_driver() -> None:
    """Stop scanning and fence live rounds without changing the user's RUN intent."""
    _STOP.set()
    goal_recovery.stop_running(tuple(_RUNNING), OWNER_ID)
