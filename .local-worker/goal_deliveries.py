"""Accepted evidence becomes a real handover; returned work keeps its one rework slot."""
from __future__ import annotations

from pathlib import Path
from typing import Final

import control_bootstrap
import delegation_policy
from goal_brain_contract import BrainError, Record
import model_registry as registry
import task_handover

MAX_RECOVERY_METADATA_BYTES: Final = 2 * 1024 * 1024
MAX_RECOVERY_TASKS: Final = 500

def accepted_handover(run: Path, notes: str, receipt: str, request_id: str = '') -> None:
    """Reuse validated review evidence; file hashes still come from the handover core."""
    review = delegation_policy.load(run)
    if review is None or review['state'] not in delegation_policy.ACCEPTED_STATES:
        raise BrainError('未通过独立验收，不能生成供后续阶段使用的交接')
    evidence = []
    for criterion in review['criteria']:
        for entry in criterion.get('evidence', []):
            if entry.get('source') in delegation_policy.INDEPENDENT_SOURCES:
                evidence.append({**entry, 'name': criterion['text']})
    if not evidence:
        raise BrainError('缺少主脑独立核验证据，不能自动生成交接')
    task_handover.create(run, summary=notes or str(review.get('reviewer_notes') or ''),
        test_evidence=evidence, context=[receipt, *(['goal-action:' + request_id] if request_id else [])], actor='main')


def recovered_handover(run: Path, project: str, goal_id: str, request_id: str) -> Record | None:
    """Only this action's still-valid, independently verified handover proves its effect."""
    record = task_handover.load(run)
    if (record is None or record.get('project') != project or record.get('goal_id') != goal_id
            or record.get('task_id') != run.name
            or 'goal-action:' + request_id not in record.get('context', [])
            or not record.get('verification', {}).get('evidence_independent')):
        return None
    if not task_handover.verify(run, record)['valid']:
        return None
    return {'task_id': run.name, 'handover_created': True}


def recovered_dispatch(project: str, goal_id: str, stage_id: str,
                       operation_id: str, request_id: str, epoch: int,
                       payload_digest: str) -> Record | None:
    """A durable attributed task is evidence of creation, never proof its model started."""
    control_bootstrap.bind_source()
    from model_desk_control.host_initialization_models import InitialKey
    import task_initialization
    task_initialization.recover(InitialKey(
        project=project, goal_id=goal_id, stage_id=stage_id, round_id=operation_id,
        request_id=request_id, epoch=epoch, payload_digest=payload_digest))
    from model_desk_control.host_trace_models import TaskObservation
    from model_desk_control.native_output import METADATA_BYTES, NativeReadError, owned_path, read_metadata
    from pydantic import ValidationError
    from pydantic import TypeAdapter
    import task_queue
    import stage_store
    stage = stage_store.get_stage(stage_id, project)
    if stage is None or stage['goal_id'] != goal_id:
        raise BrainError('原派发阶段不存在或归属不符，不能用其他阶段恢复')
    if len(stage['task_ids']) > MAX_RECOVERY_TASKS:
        raise BrainError('原阶段的任务关联超过核对窗口，未在不完整窗口中选择回执')
    matches = []
    remaining = MAX_RECOVERY_METADATA_BYTES
    for task_id in stage['task_ids']:
        try:
            path = owned_path(Path(project), Path('.model-desk/runs') / str(task_id) / 'task.json')
            raw = read_metadata(path, byte_limit=min(remaining, METADATA_BYTES))
            remaining -= len(raw.encode())
            task = TaskObservation.model_validate_json(raw)
        except (OSError, NativeReadError, ValidationError, UnicodeError) as error:
            raise BrainError('原任务记录不可核对，未改读其他项目或生成替代任务') from error
        if (task.project == project and task.goal_id == goal_id
                and task.stage_id == stage_id and task.id == task_id
                and task.authority_epoch == epoch
                and task.goal_round_id == operation_id
                and task.goal_dispatch_request_id == request_id
                and task.goal_action_payload_digest == payload_digest):
            saved_task = TypeAdapter(Record).validate_json(raw)
            queued = task_queue.observed(saved_task)
            managed = task_queue.enabled(saved_task)
            if managed or queued is not None:
                if not managed or queued is None or queued.ticket != task_queue.load_input(project, task_id).ticket:
                    raise BrainError('原任务的队列准备回执不完整，保留原动作待核对，不能重建任务')
            matches.append(str(task_id))
    if len(matches) > 1:
        raise BrainError('原派发动作有多个冲突任务，保持未知，未选择其中任意一个')
    return {'task_id': matches[0]} if matches else None


def rework_request(project: str, goal_id: str, stage: Record) -> Record:
    """The latest real returned task remains the original model and bounded lineage."""
    for task_id in reversed(stage.get('task_ids', [])):
        run = registry.task_dir(str(task_id))
        task = registry.read_task(run)
        if (task.get('project') != project or task.get('goal_id') != goal_id
                or task.get('stage_id') != stage['id']):
            raise BrainError('整改来源的项目、目标或阶段不一致，未派发新任务')
        review = delegation_policy.load(run)
        if review is None or review['state'] != delegation_policy.RETURNED:
            continue
        if (review.get('rework') or {}).get('dispatched_task_id'):
            raise BrainError('此阶段的整改已派发，不会重复占用或重置整改次数')
        if delegation_policy.brain_identity(task)[0]:
            raise BrainError('此整改任务属于其他主脑会话，需要原所有者授权')
        correction = review['rework']
        return {'route_mode': 'manual', 'rework_of': str(task_id),
                'reuse_session': stage.get('session_policy') == 'reuse_session',
                'prompt': '请按独立验收意见整改。\n问题：' + str(correction['problems'])
                          + '\n边界：' + str(correction['boundaries'])}
    raise BrainError('退回阶段缺少可验证的原整改任务，不会创建无次数限制的新任务')
