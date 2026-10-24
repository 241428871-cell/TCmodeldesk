"""Read system process identity before releasing any ambiguous native intent."""
from __future__ import annotations

import continuation_clock
import continuation_rounds
import continuation_store
from goal_brain_contract import Record
import goal_brain_runner
import stage_store


def stop_running(goals: tuple[tuple[str, str], ...], driver_id: str) -> None:
    """Fence the known driver rounds before stopping their exact native processes."""
    for project, goal_id in goals:
        intent = continuation_rounds.suspend_owned(project, goal_id, driver_id)
        if intent is not None and intent.get('pid') and intent.get('birth'):
            goal_brain_runner.stop_owned(int(intent['pid']), float(intent['birth']))


def inspect_goal(project: str, goal_id: str) -> Record:
    """Report exact process identity; graph effects require more than an exit receipt."""
    clock = continuation_clock.snapshot(project, goal_id)
    intent = clock.get('active_intent')
    if not intent:
        return {**clock, 'process_inspection': {'state': 'none', 'reason': '当前没有待核对的原生轮次'}}
    pid = intent.get('pid')
    birth = intent.get('birth')
    if type(pid) is not int or pid <= 0 or type(birth) not in (int, float) or birth <= 0:
        return {**clock, 'process_inspection': {'state': 'unknown',
            'reason': '原轮次未留下可验证的 PID 和创建时间，不能确认是否已启动，保持 UNKNOWN'}}
    verdict, reason = goal_brain_runner.inspect_owned(pid, birth)
    inspection = {'state': verdict, 'reason': reason, 'pid': pid, 'birth': birth,
                  'intent_id': intent['id']}
    if verdict != 'exited':
        return {**clock, 'process_inspection': inspection}
    with continuation_store.readonly(project) as database:
        if database is not None and database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='goal_graph_steps'"
        ).fetchone() is not None and database.execute(
            "SELECT 1 FROM goal_graph_steps WHERE intent_id=? AND step='native'", (intent['id'],)
        ).fetchone() is not None:
            inspection['reason'] += '；进程退出不代表原生操作或批次动作已核对，保留原轮次与回执'
            return {**clock, 'process_inspection': inspection}
    # Clock core checks the active intent and the process table again inside
    # its transaction, so a simultaneous Stop/new control cannot be overwritten.
    reconciled = continuation_clock.control(project, goal_id, 'reconcile', {
        'intent_id': intent['id'], 'worker_exited': True,
        'evidence': '本服务核对系统进程表：' + reason + '；原 PID=' + str(pid) + '，创建时间=' + str(birth)})
    return {**reconciled, 'process_inspection': inspection}


def settle_terminal(project: str, goal_id: str) -> Record:
    """Finish only durable bookkeeping after completion; never invoke a brain."""
    with continuation_store.locked(project, goal_id):
        goal = stage_store.get_goal(goal_id, project)
        clock = continuation_clock.snapshot(project, goal_id)
        if goal['status'] != stage_store.GOAL_COMPLETED or not clock['exists'] or clock['closed']:
            return {**clock, 'terminal_settlement': {'state': 'unchanged',
                'reason': '仅核对已完成、尚未结项的正式目标时钟'}}
        inspection = {}
        if clock.get('active_intent'):
            inspected = inspect_goal(project, goal_id)
            inspection = inspected['process_inspection']
            clock = inspected
            if clock.get('active_intent'):
                reason = '目标已完成，原轮次尚未确认退出：' + str(inspection['reason'])
                if not clock['hold']:
                    clock = continuation_clock.control(project, goal_id, 'hold',
                        {'kind': 'unknown', 'reason': reason, 'guard_id': clock['active_intent']['id']})
                return {**clock, 'process_inspection': inspection,
                        'terminal_settlement': {'state': 'waiting_process', 'reason': reason}}
        pending = [item['title'] for item in clock['obligations'] if item['status'] == 'pending']
        if pending:
            reason = '目标已有完成记录，仍需核对收尾义务：' + '；'.join(pending)
            if not clock['hold']:
                clock = continuation_clock.control(project, goal_id, 'hold', {'kind': 'approval', 'reason': reason})
            return {**clock, 'process_inspection': inspection,
                    'terminal_settlement': {'state': 'pending_obligations', 'reason': reason}}
        closed = continuation_clock.control(project, goal_id, 'close')
        return {**closed, 'process_inspection': inspection,
                'terminal_settlement': {'state': 'closed', 'reason': '已核对原轮次退出和收尾义务，正式时钟已结项'}}
