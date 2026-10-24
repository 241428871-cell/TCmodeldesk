"""Apply native-brain proposals through the same audited task/review core."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol

import continuation_clock
import delegation_policy
import dynamic_plan
import goal_effects
import human_decisions
import model_registry as registry
import stage_store
import task_graph_bridge
from controller_errors import DispatchRefused, ReviewRefused
from goal_brain_contract import BrainError, Record
from goal_deliveries import (
    accepted_handover,
    recovered_dispatch,
    recovered_handover,
    rework_request,
)
from governance_context import DispatchBlocked
from plan_contracts import DecisionRequest, PlanPatch
from plan_validation import FIELDS

if TYPE_CHECKING:
    from model_desk_control.host_effects import EffectRequest
    from model_desk_control.host_flow import HostRef


class Panel(Protocol):
    def dispatch_stage(self, stage_id: str, body: Record, project: str | None = None,
                       *, goal_effect: EffectRequest | None = None) -> Record: ...
    def update_review(self, task_id: str, body: Record, *, request_id: str = '', event_intent_id: str = '') -> Record: ...
    def cancel_task(self, task_id: str, reason: str = '', actor: str = 'main') -> Record: ...
    def resume_queued(self, project: str, goal_id: str) -> None: ...


def guard_for_dispatch(project: str, goal_id: str, stage_id: str = '',
                       authority_epoch: int | None = None) -> Record:
    guard = continuation_clock.dispatch_guard(project, goal_id, stage_id, authority_epoch=authority_epoch)
    if guard['allowed'] and stage_id and human_decisions.pending_for_stage(project, stage_id):
        return {**guard, 'allowed': False, 'reason': '此阶段存在等待人类回答的例外审核'}
    return guard


def require_runnable_evidence(review: Record, permission_mode: str) -> None:
    """Only a native auto brain can have run the test it cites as independent evidence."""
    if permission_mode != 'auto' and any(
            isinstance(entry, dict) and entry.get('source') == 'reviewer_test'
            for row in review.get('criteria', []) if isinstance(row, dict)
            for entry in row.get('evidence', []) or []):
        raise BrainError('只读主脑不能运行测试，不能提交 reviewer_test 证据')


@contextmanager
def _action_guard(
    project: str, goal_id: str, epoch: int, scope: HostRef | None
) -> Iterator[None]:
    """Keep each effect on both the human authority and claimed requirement revision."""
    with continuation_clock.authority_guard(project, goal_id, epoch):
        if scope is not None:
            continuation_clock.require_requirements_current(
                project, goal_id, scope.operation_id, epoch
            )
        yield


def _review(project: str, goal_id: str, value: Record, panel: Panel, receipt: str,
            scope: HostRef | None, index: int, permission_mode: str = 'read_only') -> None:
    task_id = str(value.get('task_id') or '')
    require_runnable_evidence(value, permission_mode)
    run = registry.task_dir(task_id)
    state = registry.read_task(run)
    if state.get('project') != project or state.get('goal_id') != goal_id:
        raise BrainError('主脑只能验收当前项目和目标中的任务')
    if not state.get('stage_id') or not isinstance(state.get('delegation'), dict):
        raise BrainError('目标任务缺少独立验收记录，不能按执行器完成直接通过')
    if delegation_policy.brain_identity(state)[0]:
        raise BrainError('此任务由其他原生主脑会话所有，需要其已验证的所有者审核')
    decision = value.get('decision')
    if decision not in {'accept', 'return'}:
        raise BrainError('主脑验收动作无效')
    body = {**value, 'action': decision, 'actor': 'main',
            'failed_criteria': [row.get('id') for row in value.get('criteria', [])
                                if isinstance(row, dict) and row.get('status') == 'fail']}
    request = goal_effects.prepare(scope, 'review:' + str(index), 'review', value)
    def perform() -> Record:
        outcome = (panel.update_review(task_id, body, request_id=request.request_id,
                                      event_intent_id=scope.operation_id if scope else '')
                   if request is not None else panel.update_review(task_id, body))
        return {'task_id': task_id, 'review': outcome['review']}
    def recover() -> Record | None:
        found = delegation_policy.find_request(run, request.request_id) if request is not None else None
        if found is None or found.get('last_request_id') != request.request_id:
            return None
        return {'task_id': task_id, 'review': found}
    goal_effects.external(project, request, value,
        goal_effects.ExternalCall(perform, recover, reject_on=(ReviewRefused,)))
    if decision == 'accept':
        payload = {'task_id': task_id, 'notes': str(value.get('notes') or ''), 'receipt': receipt}
        handover = goal_effects.prepare(scope, 'handover:' + str(index), 'goal_handover', payload)
        def deliver() -> Record:
            accepted_handover(run, payload['notes'], receipt, handover.request_id if handover else '')
            return {'task_id': task_id, 'handover_created': True}
        goal_effects.external(project, handover, payload, goal_effects.ExternalCall(deliver,
            lambda: recovered_handover(run, project, goal_id, handover.request_id) if handover else None))
        if handover is not None and recovered_handover(run, project, goal_id, handover.request_id) is None:
            raise BrainError('原验收交接已缺失或失效，未按新文件内容重新生成通过证据')


def _fulfill_obligations(project: str, goal_id: str, values: list[Record], scope: HostRef | None) -> None:
    """AI may attest to existing obligations, never cancel or redefine human work."""
    existing = {item['id']: item for item in continuation_clock.snapshot(project, goal_id)['obligations']}
    seen: set[str] = set()
    for value in values:
        obligation_id = value.get('id')
        evidence = value.get('evidence')
        if (set(value) != {'id', 'status', 'evidence'} or value.get('status') != 'fulfilled'
                or not isinstance(obligation_id, str) or obligation_id not in existing
                or obligation_id in seen or not isinstance(evidence, str) or not evidence.strip()):
            raise BrainError('收尾义务只能按既有唯一 id 提交完成核验证据；AI 不能新增、取消或降低义务')
        if existing[obligation_id]['status'] == 'cancelled':
            raise BrainError('此收尾义务已由人类取消，主脑不能改写人类决定')
        seen.add(obligation_id)
    for value in values:
        if existing[value['id']]['status'] == 'pending':
            effect = goal_effects.prepare(scope, 'obligation:' + value['id'], 'goal_obligation', value)
            continuation_clock.control(project, goal_id, 'obligation', value, effect=effect)


def _dispatch(project: str, goal_id: str, stage: Record, epoch: int, panel: Panel,
              policy_context: Record, scope: HostRef | None, key: str) -> str:
    payload = {'stage_id': stage['id'], 'stage': {field: stage[field] for field in FIELDS},
               'authority_epoch': epoch}
    effect = goal_effects.prepare(scope, key, 'dispatch', payload)
    def perform() -> Record:
        rework = stage['status'] == stage_store.RETURNED
        if not stage_store.dispatch_precheck(stage['id'], mode='rework' if rework else 'auto', project=project)['ready']:
            return {'task_id': '', 'skipped': True}
        request = {'route_mode': 'auto', 'authority_epoch': epoch,
                   'engineering_policy': policy_context, 'prompt': stage['objective']}
        if rework:
            request.update(rework_request(project, goal_id, stage))
        outcome = (panel.dispatch_stage(stage['id'], request, project=project, goal_effect=effect)
                   if effect is not None else panel.dispatch_stage(stage['id'], request, project=project))
        return {'task_id': outcome['task']['id']}
    outcome = goal_effects.external(project, effect, payload, goal_effects.ExternalCall(perform,
        lambda: recovered_dispatch(project, goal_id, stage['id'], scope.operation_id,
                    effect.request_id, epoch, effect.payload_digest) if effect and scope else None,
        reject_on=(DispatchRefused, DispatchBlocked)))
    task_id = str(outcome.get('task_id') or '')
    if task_id and effect is not None and scope is not None:
        original = recovered_dispatch(project, goal_id, stage['id'], scope.operation_id,
            effect.request_id, epoch, effect.payload_digest)
        if original is None or original['task_id'] != task_id:
            raise BrainError('派发回执与真实原任务不一致，未以历史回执替代任务证明')
    return task_id


def apply(project: str, goal_id: str, epoch: int, actions: Record, panel: Panel,
          policy_context: Record, receipt: str, contract_revision: int | None = None,
          *, scope: HostRef | None = None, plan_revision: int | None = None,
          permission_mode: str = 'read_only') -> Record:
    """Each mutation is fenced against Stop; no completion event reschedules a clock."""
    dispatched = []
    patch = (PlanPatch.model_validate({**actions['plan'], 'authority_epoch': epoch})
             if actions['plan'] is not None else None)
    plan_effect = goal_effects.prepare(scope, 'plan', 'goal_plan',
        patch.model_dump(exclude={'brief'} if patch.brief is None else set())) if patch else None
    with _action_guard(project, goal_id, epoch, scope):
        if scope is not None:
            active = continuation_clock.snapshot(project, goal_id).get('active_intent') or {}
            if (scope.project != project or scope.goal_id != goal_id or scope.epoch != epoch
                    or active.get('id') != scope.operation_id or active.get('owner_id') != scope.owner_id):
                raise BrainError('动作批次不属于当前原生轮次，未执行副作用')
        current = dynamic_plan.describe_goal(project, goal_id)
        if contract_revision is not None and current['contract_revision'] != contract_revision:
            raise BrainError('人类固定准则在本轮运行期间已更新，旧主脑提案未应用，请按新准则重新检查')
        if (plan_revision is not None and current['plan_revision'] != plan_revision
                and goal_effects.committed_revision(plan_effect) != current['plan_revision']):
            raise BrainError('计划已由其他动作修订，原批次不能根据新阶段继续执行旧提案')
        goal_effects.bind_batch(scope, {'actions': actions, 'contract_revision': contract_revision,
                                      'plan_revision': plan_revision})
    for index, review in enumerate(actions['reviews']):
        with _action_guard(project, goal_id, epoch, scope):
            _review(project, goal_id, review, panel, receipt, scope, index, permission_mode)
    with _action_guard(project, goal_id, epoch, scope):
        stage_store.sync_from_tasks(goal_id, project=project)
        if patch is not None:
            dynamic_plan.revise_plan(project, goal_id, patch, effect=plan_effect)
        plan = dynamic_plan.describe_goal(project, goal_id)
        by_key = {str(row.get('key') or row['id']): row['id'] for row in plan['stages']}
        for index, question in enumerate(actions['questions']):
            payload = {**question, 'affected_stage_ids': [by_key.get(key, key)
                       for key in question['affected_stage_ids']]}
            parsed = DecisionRequest.model_validate(payload)
            effect = goal_effects.prepare(scope, 'question:' + str(index), 'goal_question', parsed.model_dump())
            human_decisions.create_decision(project, goal_id, parsed, effect=effect)
    def launch(stage: Record, key: str) -> str:
        return _dispatch(project, goal_id, stage, epoch, panel, policy_context, scope, key)
    if scope is not None:
        # Answered exceptions resume only their own branch, before this batch's new requests.
        with _action_guard(project, goal_id, epoch, scope):
            for outcome in task_graph_bridge.resume_waiting(scope, launch):
                if outcome.task_id and outcome.task_id not in dispatched:
                    dispatched.append(outcome.task_id)
    for index, requested in enumerate(actions['dispatch_stage_ids']):
        stage_id = str(by_key.get(requested, requested))
        with _action_guard(project, goal_id, epoch, scope):
            stage = stage_store.get_stage(stage_id, project)
            if stage is None or stage['goal_id'] != goal_id:
                raise BrainError('主脑派发的阶段不属于当前目标')
            if scope is None:
                guard = guard_for_dispatch(project, goal_id, stage_id, epoch)
                if not guard['allowed']:
                    continue
                task_id = launch(stage, 'dispatch:' + str(index))
            else:
                # A pending exception parks this branch durably; sibling branches continue.
                task_id = task_graph_bridge.run_requested(
                    scope, stage_id, 'dispatch:' + str(index), launch).task_id
            if task_id and task_id not in dispatched:
                dispatched.append(task_id)
    completed = False
    with _action_guard(project, goal_id, epoch, scope):
        _fulfill_obligations(project, goal_id, actions['obligations'], scope)
        pending_obligations = [item['id'] for item in continuation_clock.snapshot(project, goal_id)['obligations']
                               if item['status'] == 'pending']
        if actions['complete'] and not pending_obligations:
            stage_store.sync_from_tasks(goal_id, project=project)
            gate = human_decisions.final_acceptance_ready(project, goal_id)
            if gate['required'] and not gate['approved']:
                human_decisions.request_final_review(project, goal_id, [receipt], effect_scope=scope)
            elif gate['approved']:
                effect = goal_effects.prepare(scope, 'complete', 'goal_complete', {'status': stage_store.GOAL_COMPLETED})
                stage_store.set_goal_status(goal_id, stage_store.GOAL_COMPLETED, actor='goal-brain', project=project, effect=effect)
                completed = True
    summary = str(actions['summary'])
    if actions['complete'] and pending_obligations:
        summary += '；收尾义务尚未全部核验，目标和原续跑周期保持进行中'
    return {'dispatched_task_ids': dispatched, 'goal_completed': completed,
            'pending_obligation_ids': pending_obligations, 'summary': summary}
