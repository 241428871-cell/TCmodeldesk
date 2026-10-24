"""Leased round receipts; an ambiguous launch is never automatically replayed."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import continuation_store as store
from continuation_store import ClockError, RequirementsChangedError
import goal_settlement
import goal_requirements

if TYPE_CHECKING:
    from model_desk_control.host_settlement import SettlementRequest


def _running(clock: dict) -> None:
    if not clock['enabled'] or clock['user_intent'] != 'RUN' or clock['closed'] or clock['hold']:
        raise ClockError('当前续跑授权已暂停、停止或等待保护解除')


def claim_due(project: str, goal_id: str, owner_id: str, lease_seconds: int = 120,
              now: float | None = None, *, host_protocol: Literal['legacy', 'graph_v2'] = 'legacy') -> dict | None:  # noqa: DICT_OK - durable intent boundary.
    """Claim at most one due intent; missed periods coalesce and never accelerate."""
    current = time.time() if now is None else now
    seconds = store.interval(lease_seconds)
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ClockError('续跑领取需要明确 owner_id')
    claimed = None
    with store.mutation(project, goal_id) as conn:
        clock = store.read(conn, goal_id)
        if clock is None or not clock['enabled'] or clock['user_intent'] != 'RUN' or clock['closed'] or clock['hold']:
            return None
        active = store.active(conn, clock)
        if active:
            if store.timestamp(active['lease_until']) <= current:
                active['status'] = 'unknown'
                clock['hold'] = {'kind': 'unknown', 'reason': '旧轮次租约过期，须核对原进程与回执，不能盲目重启',
                                 'guard_id': active['id'], 'at': store.iso(current), 'source': 'lease_expired'}
                store.write_intent(conn, active)
                store.write(conn, clock)
            return None
        identity = 'round_' + uuid4().hex
        event_ids = []
        due = store.timestamp(clock['next_due_at']) <= current
        event_allowed = not clock.get('event_not_before') or store.timestamp(clock['event_not_before']) <= current
        goal = conn.execute('SELECT status FROM goals WHERE id=?', (goal_id,)).fetchone()
        if host_protocol == 'graph_v2' and goal is not None and goal['status'] == 'active' and (due or event_allowed):
            import goal_events
            event_ids = goal_events.claim(conn, clock['project'], goal_id, clock['authority_epoch'], identity)
        if not due and not event_ids:
            return None
        claimed = {'id': identity, 'intent_id': identity, 'goal_id': goal_id,
                   'authority_epoch': clock['authority_epoch'], 'owner_id': owner_id,
                   'owner_epoch': clock.get('owner_epoch', 0),
                   'status': 'claimed', 'created_at': store.iso(current),
                   'lease_until': store.iso(current + seconds), 'pid': None, 'birth': None,
                   'brain_model_id': clock['brain_model_id'], 'native_session_id': clock['native_session_id'],
                   'brain_permission_mode': clock.get('brain_permission_mode', 'read_only'),
                   'requirements_revision': goal_requirements.latest_version(conn, goal_id)}
        claimed['host_protocol'] = host_protocol
        claimed['trigger_kind'] = 'clock' if due else 'event'
        claimed['event_ids'] = event_ids
        clock['active_intent_id'] = identity
        if due:
            clock['next_due_at'] = store.iso(current + clock['interval_seconds'])
            clock.pop('event_not_before', None)
        store.write_intent(conn, claimed)
        store.write(conn, clock)
    return claimed


def record_runner(project: str, goal_id: str, intent_id: str, epoch: int, pid: int,
                  birth: float, native_session_id: str = '') -> store.IntentRecord:
    """Bind a native worker's exact PID and start identity before accepting output."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not isinstance(birth, (float, int)) or birth <= 0:
        raise ClockError('工作者需要有效 PID 和进程启动时间')
    with store.mutation(project, goal_id) as conn:
        clock = store.read(conn, goal_id)
        record = store.intent(conn, intent_id)
        store.require_epoch(clock, record, epoch)
        _running(clock)
        if record['status'] not in ('claimed', 'started') or store.timestamp(record['lease_until']) <= time.time():
            raise ClockError('领取已过期或状态不明，禁止启动另一工作者')
        if record.get('pid') and (record['pid'] != pid or record['birth'] != birth):
            raise ClockError('此轮次已有另一个工作者，不能重复绑定')
        record.update(status='started', pid=pid, birth=birth)
        if native_session_id:
            record['native_session_id'] = str(native_session_id)
            clock['native_session_id'] = str(native_session_id)
        store.write_intent(conn, record)
        store.write(conn, clock)
    return record


def record_session(project: str, goal_id: str, intent_id: str, epoch: int,
                   native_session_id: str) -> store.IntentRecord:
    """Persist the provider's actual session receipt without changing its identity."""
    if not isinstance(native_session_id, str) or not native_session_id.strip():
        raise ClockError('原生会话回执不能为空')
    with store.mutation(project, goal_id) as conn:
        clock = store.read(conn, goal_id)
        record = store.intent(conn, intent_id)
        store.require_epoch(clock, record, epoch)
        _running(clock)
        record['native_session_id'] = native_session_id
        clock['native_session_id'] = native_session_id
        store.write_intent(conn, record)
        store.write(conn, clock)
    return record


def renew_lease(project: str, goal_id: str, intent_id: str, epoch: int,
                lease_seconds: int = 120) -> store.IntentRecord:
    """Extend only the current owner; lease renewal never changes continuation cadence."""
    seconds = store.interval(lease_seconds)
    with store.mutation(project, goal_id) as conn:
        clock = store.read(conn, goal_id)
        record = store.intent(conn, intent_id)
        store.require_epoch(clock, record, epoch)
        _running(clock)
        if record['status'] not in ('claimed', 'started'):
            raise ClockError('未知或终止轮次不能自行续租')
        record['lease_until'] = store.iso(time.time() + seconds)
        store.write_intent(conn, record)
    return record


def require_requirements_current(
    project: str, goal_id: str, intent_id: str, epoch: int,
    *, expected_revision: int | None = None,
) -> None:
    """Fence one action boundary against requirements inserted after the claim."""
    with store.locked(project, goal_id), store.readonly(project) as conn:
        if conn is None:
            raise ClockError("目标续跑记录不存在，不能应用主脑动作")
        clock = store.read(conn, goal_id)
        record = store.intent(conn, intent_id)
        store.require_epoch(clock, record, epoch)
        claimed = int(record.get("requirements_revision", 0))
        if expected_revision is not None and claimed != expected_revision:
            raise ClockError("任务保存的需求版本与原主脑轮次不一致，旧副作用未应用")
        current = goal_requirements.latest_version(conn, goal_id)
        advertised = int(clock.get("requirements_revision", 0))
        if current != advertised:
            raise ClockError("需求版本记录不一致，旧主脑动作未应用")
        if current != claimed:
            raise RequirementsChangedError(
                f"运行中需求已从 R{claimed} 更新为 R{current}，旧主脑动作未应用；下一安全轮次将重新规划"
            )


def require_task_requirements_current(
    project: str, goal_id: str, intent_id: str, epoch: int, expected_revision: int
) -> None:
    """Fence a queued or completed-round task without reviving its old brain ownership."""
    with store.locked(project, goal_id), store.readonly(project) as conn:
        if conn is None:
            raise ClockError("目标续跑记录不存在，旧任务副作用未应用")
        clock = store.read(conn, goal_id)
        record = store.intent(conn, intent_id)
        if (
            clock is None
            or record is None
            or record.get("goal_id") != goal_id
            or record.get("authority_epoch") != epoch
            or clock.get("authority_epoch") != epoch
            or record.get("owner_epoch", 0) != clock.get("owner_epoch", 0)
        ):
            raise ClockError("任务所属主脑轮次或授权代次已变化，旧副作用未应用")
        claimed = int(record.get("requirements_revision", 0))
        if claimed != expected_revision:
            raise ClockError("任务保存的需求版本与原主脑轮次不一致，旧副作用未应用")
        current = goal_requirements.latest_version(conn, goal_id)
        if current != int(clock.get("requirements_revision", 0)):
            raise ClockError("需求版本记录不一致，旧任务副作用未应用")
        if current != claimed:
            raise RequirementsChangedError(
                f"运行中需求已从 R{claimed} 更新为 R{current}，旧任务结果未写回；下一安全轮次将重新规划"
            )


def finish_round(project: str, goal_id: str, intent_id: str, epoch: int,
                 result: dict, *, settlement: SettlementRequest | None = None) -> store.ClockSnapshot:
    """Record one round; a completed turn does not close its goal or withdraw its clock."""
    if not isinstance(result, dict) or result.get('status') not in (
        'completed', 'superseded', 'blocked', 'failed', 'unknown'
    ):
        raise ClockError('续跑结果须明确 completed、superseded、blocked、failed 或 unknown')
    with store.mutation(project, goal_id) as conn:
        clock = store.read(conn, goal_id)
        record = store.intent(conn, intent_id)
        if settlement is not None:
            if record is None or settlement.scope.operation_id != intent_id or settlement.scope.epoch != epoch:
                raise ClockError('结算回执不属于原轮次')
            goal_settlement.verify(settlement, project, goal_id, record, result)
        if settlement is not None and record['status'] == settlement.result.status:
            goal_settlement.verify(settlement, project, goal_id, record, record['result'])
            goal_settlement.commit(conn, settlement)
        else:
            store.require_epoch(clock, record, epoch)
            if record['status'] not in ('claimed', 'started'):
                raise ClockError('此轮次已结束或状态未知，不能重复提交')
            status = result['status']
            record.update(status=status, finished_at=store.iso(), result=result)
            clock['last_result'] = result
            clock['last_round_at'] = record['finished_at']
            if result.get('native_session_id'):
                clock['native_session_id'] = str(result['native_session_id'])
            own_hold = (not clock['hold'] or
                        (clock['hold'].get('guard_id') == intent_id and clock['hold'].get('source')
                         in {'round_result', 'lease_expired', 'service_shutdown'}))
            if status == 'unknown':
                if own_hold:
                    clock['hold'] = {'kind': 'unknown', 'reason': result.get('summary', '原生工作者结果不明'),
                                     'guard_id': intent_id, 'at': store.iso(), 'source': 'round_result'}
                if record.get('recovery'):
                    record['recovery']['status'] = 'blocked'
            else:
                if record.get('recovery') and status == 'completed':
                    record['recovery']['status'] = 'completed'
                clock['active_intent_id'] = ''
                if status == 'blocked' or (status == 'failed' and result.get('retryable') is not True):
                    if own_hold:
                        clock['hold'] = {'kind': 'approval' if status == 'blocked' else 'error',
                                         'reason': result.get('summary', '需要主脑或人类解决后继续'), 'at': store.iso()}
                elif status == 'failed':
                    seconds = min(31536000, clock['interval_seconds'] * 2)
                    clock['interval_seconds'] = seconds
                    clock['next_due_at'] = store.iso(max(store.timestamp(clock['next_due_at']), time.time() + seconds))
                    clock['event_not_before'] = clock['next_due_at']
                elif status == 'superseded':
                    clock['next_due_at'] = store.iso()
                    clock.pop('event_not_before', None)
            store.write_intent(conn, record)
            store.write(conn, clock)
            if status == 'completed':
                goal_requirements.mark_applied(
                    conn, goal_id, int(record.get('requirements_revision', 0)), intent_id
                )
            if settlement is not None:
                goal_settlement.commit(conn, settlement)
    from continuation_clock import snapshot
    return snapshot(project, goal_id)


def suspend_owned(project: str, goal_id: str, driver_id: str) -> store.IntentRecord | None:
    """A shutting-down driver retains its own effects without revoking human RUN."""
    with store.mutation(project, goal_id) as conn:
        clock = store.read(conn, goal_id)
        record = store.active(conn, clock) if clock is not None else None
        if record is None:
            return None
        recovery = record.get('recovery') or {}
        if (record['owner_id'] != driver_id
                and not (recovery.get('driver_id') == driver_id and recovery.get('status') == 'running')):
            return None
        if clock['user_intent'] == 'RUN' and clock['enabled'] and not clock['closed'] and not clock['hold']:
            record['status'] = 'unknown'
            clock['hold'] = {'kind': 'unknown', 'reason': '服务关闭，保留原轮次等待回执核对',
                            'source': 'service_shutdown', 'guard_id': record['id'], 'at': store.iso()}
            store.write_intent(conn, record)
            store.write(conn, clock)
    return record


def _worker_exited(record: dict) -> bool:
    if not record.get('pid'):
        return False
    try:
        import psutil
        process = psutil.Process(record['pid'])
        return abs(process.create_time() - record['birth']) > .1 or process.status() == psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return True
    except (psutil.AccessDenied, OSError):
        return False


def reconcile(conn, clock: dict, body: dict) -> None:
    """Resolve a stopped/unknown receipt only with explicit evidence and exited worker."""
    record = store.active(conn, clock)
    if (record is None or body.get('intent_id') != record['id'] or not body.get('evidence')
            or body.get('worker_exited') is not True):
        raise ClockError('核对需要当前轮次 id、退出证明和回执证据')
    if not _worker_exited(record):
        raise ClockError('原 PID 与启动时间仍存活或无法确认，禁止重新启动')
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='goal_graph_steps'"
    ).fetchone() is not None and conn.execute(
        "SELECT 1 FROM goal_graph_steps WHERE intent_id=? AND step='native'", (record['id'],)
    ).fetchone() is not None:
        raise ClockError('原轮次已有原生阶段；仅退出证明不能核销未核对的原生操作与批次动作')
    record.update(status='reconciled', reconciled_at=store.iso(), evidence=str(body['evidence']))
    store.write_intent(conn, record)
    clock['active_intent_id'] = ''
    clock['authority_epoch'] += 1
    if clock['hold'].get('kind') == 'unknown':
        clock['hold'] = {'kind': 'error', 'reason': '旧轮次已核对退出；需明确解决授权后继续', 'at': store.iso()}


def release_unstarted(project: str, goal_id: str, intent_id: str, epoch: int,
                      owner_id: str, evidence: str) -> store.ClockSnapshot:
    """Original host receipt: it never invoked its runner. Not a human control action."""
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 8000:
        raise ClockError('未启动回执需要原宿主的明确证据')
    with store.mutation(project, goal_id) as conn:
        clock = store.read(conn, goal_id)
        record = store.intent(conn, intent_id)
        if (clock is None or record is None or clock['active_intent_id'] != intent_id
                or record['authority_epoch'] != epoch or record['owner_id'] != owner_id
                or record.get('pid') is not None or record['status'] not in ('claimed', 'revoked')):
            raise ClockError('只有原领取宿主能确认其尚未调用执行器的轮次；未知工作者不可用此回执恢复')
        record.update(status='abandoned', finished_at=store.iso(), evidence=evidence)
        store.write_intent(conn, record)
        clock['active_intent_id'] = ''
        clock['authority_epoch'] += 1
        store.write(conn, clock)
    from continuation_clock import snapshot
    return snapshot(project, goal_id)
