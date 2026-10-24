"""User-controlled durable continuation, independent of a chat turn's lifecycle."""
from __future__ import annotations
from contextlib import contextmanager
import math
import time
from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

import stage_store
import project_storage
import continuation_store as store
import goal_effects
from continuation_store import ClockError, RequirementsChangedError

if TYPE_CHECKING:
    from model_desk_control.host_effects import EffectRequest


class ControlAction(StrEnum):
    START = 'start'
    RESUME = 'resume'
    RUN_NOW = 'run_now'
    PAUSE = 'pause'
    STOP = 'stop'
    BACKOFF = 'backoff'
    HOLD = 'hold'
    CLEAR_HOLD = 'clear_hold'
    OBLIGATION = 'obligation'
    RECONCILE = 'reconcile'
    CLOSE = 'close'


def snapshot(project: str, goal_id: str) -> dict:  # noqa: DICT_OK - public JSON boundary.
    """Read current clock and owner without creating records or starting work."""
    project = project_storage.normalize(project)
    with store.readonly(project) as conn:
        clock = store.read(conn, goal_id) if conn is not None else None
        if clock is None:
            return {'exists': False, 'project': project, 'goal_id': goal_id,
                    'authority_epoch': 0, 'user_intent': 'RUN', 'enabled': False}
        clock['active_intent'] = store.active(conn, clock)
        clock['status'] = _status(clock)
        return clock


def _status(clock: dict) -> str:
    if clock['closed']:
        return 'closed'
    if clock['user_intent'] == 'STOP':
        return 'stopped'
    if clock['user_intent'] == 'USER_PAUSE' or not clock['enabled']:
        return 'paused'
    if clock['hold']:
        return 'held'
    if clock.get('active_intent'):
        return 'running'
    return 'due' if store.timestamp(clock['next_due_at']) <= time.time() else 'waiting'


def dispatch_guard(project: str, goal_id: str, stage_id: str = '', authority_epoch: int | None = None) -> dict:  # noqa: DICT_OK
    """Check live authority; legacy manual goals retain their existing workflow."""
    clock = snapshot(project, goal_id)
    reason = ''
    if clock['exists']:
        if clock['closed']:
            reason = '目标已结项'
        elif clock['user_intent'] == 'STOP':
            reason = '用户已 STOP，旧授权不可恢复'
        elif clock['user_intent'] == 'USER_PAUSE' or not clock['enabled']:
            reason = '用户已暂停自动续跑'
        elif clock['hold']:
            reason = clock['hold'].get('reason', '当前任务等待解除保护')
        elif authority_epoch is not None and authority_epoch != clock['authority_epoch']:
            reason = '续跑授权代次已变化'
    elif authority_epoch not in (None, 0):
        reason = '续跑记录不存在，不能使用旧轮次授权'
    return {'allowed': not reason, 'reason': reason, 'exists': clock['exists'],
            'authority_epoch': clock['authority_epoch'], 'goal_id': goal_id, 'stage_id': stage_id}


guard_for_dispatch = dispatch_guard


@contextmanager
def authority_lock(project: str, goal_id: str, epoch: int | None = None):
    """Fence one effect against concurrent STOP using the same durable control lock."""
    with store.locked(project, goal_id):
        guard = dispatch_guard(project, goal_id, authority_epoch=epoch)
        if not guard['allowed']:
            raise ClockError(guard['reason'])
        yield guard


authority_guard = authority_lock


def configure_clock(project: str, goal_id: str, interval_seconds: int = 1800,
                    enabled: bool = False, obligations: list | None = None,
                    brain_model_id: str = '', native_session_id: str = '') -> store.ClockSnapshot:
    """Explicitly configure the same clock; opening a goal does not enable it."""
    seconds = store.interval(interval_seconds)
    if not isinstance(enabled, bool):
        raise ClockError('续跑开关须为布尔值')
    project = project_storage.normalize(project)
    with store.mutation(project, goal_id) as conn:
        clock = store.read(conn, goal_id) or store.new_clock(project, goal_id, seconds)
        if clock['closed']:
            raise ClockError('已结项目标不能重新启用旧时钟')
        clock['interval_seconds'] = seconds
        if brain_model_id:
            _same_session_model(clock, brain_model_id)
            clock['brain_model_id'] = str(brain_model_id)
        if native_session_id:
            clock['native_session_id'] = str(native_session_id)
        if obligations is not None:
            _obligations(clock, obligations)
        if enabled and (clock['user_intent'] != 'RUN' or not clock['enabled']):
            _activate(clock, {})
        elif not enabled and clock['enabled']:
            _revoke(conn, clock, 'USER_PAUSE')
        store.write(conn, clock)
    return snapshot(project, goal_id)


def _obligations(clock: dict, additions: list) -> None:
    if not isinstance(additions, list) or len(additions) > 100:
        raise ClockError('义务列表格式无效或超过 100 项')
    existing = {item['id']: item for item in clock['obligations']}
    for item in additions:
        if not isinstance(item, dict) or not item.get('id') or not item.get('title'):
            raise ClockError('义务需要稳定 id 和标题')
        if item['id'] not in existing:
            existing[item['id']] = {'id': str(item['id']), 'title': str(item['title']),
                                    'status': 'pending', 'evidence': ''}
    clock['obligations'] = list(existing.values())


def _activate(clock: dict, payload: dict) -> None:
    if clock['active_intent_id']:
        raise ClockError('已有轮次尚未确认结束，请先核对原进程与回执')
    if clock['user_intent'] != 'RUN':
        clock['authority_epoch'] += 1
    clock['user_intent'], clock['enabled'] = 'RUN', True
    clock['next_due_at'] = clock['next_due_at'] or store.iso()
    if payload.get('model_id'):
        _same_session_model(clock, str(payload['model_id']))
        clock['brain_model_id'] = str(payload['model_id'])
    if payload.get('native_session_id'):
        clock['native_session_id'] = str(payload['native_session_id'])


def _same_session_model(clock: dict, selected: str) -> None:
    if clock['native_session_id'] and clock['brain_model_id'] and selected != clock['brain_model_id']:
        raise ClockError('原生会话已绑定原主脑模型；切换模型请新建目标，不能跨模型恢复旧会话')


def _revoke(conn, clock: dict, intent: str) -> None:
    clock['authority_epoch'] += 1
    clock['user_intent'], clock['enabled'] = intent, False
    record = store.active(conn, clock)
    if record:
        record['status'] = 'revoked'
        store.write_intent(conn, record)


def _close(conn, clock: dict, payload: dict) -> None:
    if clock['active_intent_id']:
        raise ClockError('仍有未核对轮次，不能结项')
    if any(item['status'] == 'pending' for item in clock['obligations']):
        raise ClockError('仍有保存、分析、报告或清理等未完成义务')
    partial = payload.get('partial') is True
    goal = conn.execute('SELECT status FROM goals WHERE id=?', (clock['goal_id'],)).fetchone()
    stages = conn.execute('SELECT status FROM stages WHERE goal_id=?', (clock['goal_id'],)).fetchall()
    if not partial and (goal['status'] != 'completed' or not stages
                        or any(row['status'] not in ('accepted', 'final_accepted', 'cancelled') for row in stages)):
        raise ClockError('正常结项需要固定准则、阶段独立验收及目标完成记录')
    if partial and not str(payload.get('evidence', '')).strip():
        raise ClockError('部分结项需要交付范围和未完成事项的证据')
    _revoke(conn, clock, 'STOP')
    clock['closed'], clock['terminal_kind'] = True, 'partial' if partial else 'completed'


def control(project: str, goal_id: str, action: str, payload: dict | None = None,
            *, effect: EffectRequest | None = None) -> dict:  # noqa: DICT_OK
    """Apply an explicit control; automatic resource clear never resumes user pause."""
    body = payload or {}
    try:
        selected = ControlAction(action)
    except ValueError as error:
        raise ClockError('未知续跑控制动作：' + str(action)) from error
    project = project_storage.normalize(project)
    if effect is not None:
        if selected is not ControlAction.OBLIGATION:
            raise ClockError('目标动作回执仅能用于既有收尾义务，不能用于修改续跑授权')
        goal_effects.verify(effect, project, goal_id, 'goal_obligation', body)
    with store.mutation(project, goal_id) as conn:
        clock = store.read(conn, goal_id)
        if clock is None:
            raise ClockError('请先配置此目标的续跑时钟')
        if clock['closed']:
            raise ClockError('目标已结项，旧控制请求失效')
        if effect is not None:
            if effect.scope.epoch != clock['authority_epoch']:
                raise ClockError('原动作的续跑授权已变化')
            replay = goal_effects.existing(conn, effect)
            if replay is not None:
                if replay.subject_id != body.get('id'):
                    raise ClockError('原收尾动作的回执身份不一致')
                return snapshot(project, goal_id)
        match selected:
            case ControlAction.START | ControlAction.RESUME:
                _activate(clock, body)
            case ControlAction.RUN_NOW:
                if clock['user_intent'] != 'RUN' or not clock['enabled'] or clock['hold']:
                    raise ClockError('立即检查不能越过暂停、STOP 或保护等待')
                if clock['active_intent_id']:
                    raise ClockError('当前轮次尚未结束，不能重复启动')
                clock['next_due_at'] = store.iso()
                clock.pop('event_not_before', None)
            case ControlAction.PAUSE:
                _revoke(conn, clock, 'USER_PAUSE')
            case ControlAction.STOP:
                _revoke(conn, clock, 'STOP')
            case ControlAction.BACKOFF:
                seconds = store.interval(body.get('interval_seconds', clock['interval_seconds'] * 2))
                if seconds < clock['interval_seconds']:
                    raise ClockError('自动退避不能缩短续跑周期；用户可在配置中明确调整')
                clock['interval_seconds'] = seconds
                previous = store.timestamp(clock['next_due_at'])
                clock['next_due_at'] = store.iso(max(previous if math.isfinite(previous) else 0, time.time() + seconds))
                clock['event_not_before'] = clock['next_due_at']
            case ControlAction.HOLD:
                kind = body.get('kind', 'error')
                if kind not in ('resource', 'error', 'approval', 'unknown') or not body.get('reason'):
                    raise ClockError('保护等待需要有效类型与明确原因')
                clock['hold'] = {'kind': kind, 'reason': str(body['reason']),
                                 'guard_id': str(body.get('guard_id', '')), 'at': store.iso()}
            case ControlAction.CLEAR_HOLD:
                hold = clock['hold']
                if hold and hold['kind'] == 'unknown':
                    raise ClockError('未知进程必须先通过 reconcile 核对身份与退出证据')
                if hold and hold.get('guard_id') and body.get('guard_id') != hold['guard_id']:
                    raise ClockError('解除保护必须绑定原保护器身份')
                if hold and hold['kind'] != 'resource' and body.get('authorized') is not True:
                    raise ClockError('错误或审核等待需要明确的解决授权')
                if hold and not body.get('evidence'):
                    raise ClockError('解除保护需要恢复证据')
                if hold and hold['kind'] == 'resource':
                    clock['event_not_before'] = clock['next_due_at']
                clock['hold'] = {}
            case ControlAction.OBLIGATION:
                item = next((item for item in clock['obligations'] if item['id'] == body.get('id')), None)
                if item is None or body.get('status') not in ('fulfilled', 'cancelled') or not body.get('evidence'):
                    raise ClockError('义务完成或取消需要有效 id、状态与证据')
                if item['status'] != 'pending' and body['status'] != item['status']:
                    raise ClockError('已完成或取消的义务不能由旧请求重新解释')
                item.update(status=body['status'], evidence=str(body['evidence']))
                goal_effects.save(conn, effect, item['id'])
            case ControlAction.RECONCILE:
                from continuation_rounds import reconcile
                reconcile(conn, clock, body)
            case ControlAction.CLOSE:
                _close(conn, clock, body)
            case _:
                assert_never(selected)
        store.write(conn, clock)
    return snapshot(project, goal_id)


from continuation_rounds import (claim_due, finish_round, record_runner, record_session,
                                 renew_lease, release_unstarted, require_requirements_current,
                                 require_task_requirements_current)
