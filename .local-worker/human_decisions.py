"""Explicit human decisions tied to criteria, plan, authority and evidence."""
from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import JsonValue

import stage_store
import goal_effects
import decision_scope
import continuation_store
import goal_events
from plan_validation import FIELDS, decode_stages, is_superseded
from plan_contracts import DecisionAnswer, DecisionRequest, FinalReview, PlanError
import plan_records as records
from decision_records import (approved_final, blocking_in_connection,
                              decision_row, has_explicit_approval)

if TYPE_CHECKING:
    from model_desk_control.host_effects import EffectRequest
    from model_desk_control.host_flow import HostRef


def create_decision(project: str, goal_id: str,
                    payload: DecisionRequest | dict[str, JsonValue],
                    actor: str = 'brain', *, effect: EffectRequest | None = None) -> dict[str, JsonValue]:
    """Submit one evidence-bound question and deduplicate the same unresolved blocker."""
    parsed = DecisionRequest.model_validate(payload)
    project = records.owner(project, goal_id)
    goal_effects.verify(effect, project, goal_id, 'goal_question', parsed.model_dump())
    with continuation_store.locked(project, goal_id), goal_effects.authority(effect), stage_store.connection(project) as conn, stage_store._transaction(conn):
        records.ensure_schema(conn)
        replay = goal_effects.existing(conn, effect)
        if replay is not None:
            original = conn.execute('SELECT * FROM human_decisions WHERE id=? AND goal_id=?',
                                    (replay.subject_id, goal_id)).fetchone()
            if original is None:
                raise PlanError('effect_subject', '原人工问题回执不存在，未创建替代问题')
            return decision_row(original)
        if parsed.kind == 'exception' and not parsed.affected_stage_ids:
            raise PlanError('decision_scope', '例外审核必须明确列出受影响阶段，其他阶段可继续')
        fingerprint = decision_scope.create_fingerprint(conn, project, goal_id, parsed)
        options = [{'id': 'option_' + records.digest(label)[:16], 'label': label} for label in parsed.options]
        affected = []
        for stage_id in dict.fromkeys(parsed.affected_stage_ids):
            selected = conn.execute('SELECT * FROM stages WHERE goal_id=? AND id=?', (goal_id, stage_id)).fetchall()
            if not selected:
                raise PlanError('decision_scope', '审核只能影响当前目标中的阶段')
            affected.extend(decode_stages(selected))
        value = {**parsed.model_dump(), 'options': options, 'approval_option_id': next(option['id'] for option in options if option['label'] == parsed.approval_option),
                 'affected_stage_digests': {row['id']: records.digest({field: row[field] for field in FIELDS})
                                            for row in affected if row['id'] in parsed.affected_stage_ids}}
        value['artifact_digest'] = fingerprint['artifact_digest']
        subject = records.digest({'goal_id': goal_id, 'proposal': value, 'fingerprint': fingerprint})
        identity = _identity(value)
        for raw in conn.execute("SELECT * FROM human_decisions WHERE goal_id=? AND status IN ('pending','answered')", (goal_id,)):
            previous = decision_row(raw)
            if _identity(previous) == identity:
                if previous['subject_hash'] == subject:
                    goal_effects.save(conn, effect, previous['id'])
                    return previous
                conn.execute("UPDATE human_decisions SET status='stale',updated_at=? WHERE id=?",
                             (records.now(), previous['id']))
        decision_id = 'decision_' + uuid4().hex
        now = records.now()
        conn.execute('INSERT INTO human_decisions(id,goal_id,kind,status,subject_hash,fingerprint,payload, '
                     'created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                     (decision_id, goal_id, parsed.kind, 'pending', subject, records.dump(fingerprint),
                      records.dump(value), now, now))
        records.log(conn, goal_id, 'human_review_requested', actor, decision_id + ': ' + parsed.question)
        goal_effects.save(conn, effect, decision_id)
        row = conn.execute('SELECT * FROM human_decisions WHERE id=?', (decision_id,)).fetchone()
        return decision_row(row)


def _identity(value: dict[str, JsonValue]) -> str:
    """Evidence updates replace one blocker rather than creating duplicate open questions."""
    return records.digest({'question': value['question'], 'kind': value['kind'],
                           'affected_stage_ids': sorted(value['affected_stage_ids'])})


def list_decisions(project: str, goal_id: str | None = None) -> list[dict[str, JsonValue]]:
    """Read only this project's inbox, including stale and answered audit history."""
    import project_storage
    project = records.owner(project, goal_id) if goal_id else project_storage.normalize(project)
    with stage_store.connection(project) as conn:
        if not records.has_table(conn, 'human_decisions'):
            return []
        query = 'SELECT * FROM human_decisions'
        rows = conn.execute(query + ' WHERE goal_id=? ORDER BY created_at DESC', (goal_id,)) if goal_id else conn.execute(
            query + ' ORDER BY created_at DESC')
        return [decision_row(row) for row in rows]


def answer_decision(project: str, decision_id: str,
                    payload: DecisionAnswer | dict[str, JsonValue],
                    actor: str = 'human') -> dict[str, JsonValue]:
    """Only an explicit human route can answer a current, unchanged subject."""
    if actor != 'human':
        raise PlanError('human_only', '主脑不能代替人类批准审核项')
    import project_storage
    parsed = DecisionAnswer.model_validate(payload)
    project = project_storage.normalize(project)
    with decision_scope.answering(project, decision_id) as bound_goal, stage_store.connection(project) as conn, stage_store._transaction(conn):
        if not records.has_table(conn, 'human_decisions'):
            raise PlanError('decision_missing', '当前项目中不存在该审核项')
        raw = conn.execute('SELECT * FROM human_decisions WHERE id=?', (decision_id,)).fetchone()
        if raw is None:
            raise PlanError('decision_missing', '当前项目中不存在该审核项')
        if raw['goal_id'] != bound_goal:
            raise PlanError('decision_scope', '审核项的目标归属已变化，请重新读取')
        if raw['status'] != 'pending':
            raise PlanError('decision_resolved', '该审核项已回答或已过期，请读取最新提案')
        current = decision_scope.current_fingerprint(conn, project, decision_scope.StoredSubject(
            raw['goal_id'], raw['payload'], raw['fingerprint'], raw['subject_hash']))
        row = decision_row(raw)
        if not has_explicit_approval(row):
            raise PlanError('decision_semantics_missing', '该审核项没有明确的继续选项定义，请主脑重新提交')
        if parsed.subject_hash != row['subject_hash'] or parsed.authority_epoch != current['authority_epoch'] or row['fingerprint'] != current:
            raise PlanError('stale_subject', '计划、准则、工程习惯、产物或续跑授权已变化；请主脑重新提交审核')
        options = row['options']
        choices = [option['id'] for option in options
                   if option['id'] == parsed.option_id or option['label'] == parsed.answer or option['id'] == parsed.answer]
        if len(set(choices)) != 1:
            raise PlanError('invalid_option', '请明确选择当前提案中的一个选项')
        answer = choices[0]
        conn.execute("UPDATE human_decisions SET status='answered',answer=?,notes=?,updated_at=? WHERE id=?",
                     (answer, parsed.notes, records.now(), decision_id))
        records.log(conn, row['goal_id'], 'human_review_answered', actor,
                    records.dump({'decision_id': decision_id, 'option_id': answer, 'notes': parsed.notes}))
        goal_events.answered(conn, project, bound_goal, decision_id)
        return decision_row(conn.execute('SELECT * FROM human_decisions WHERE id=?', (decision_id,)).fetchone())


def pending_for_stage(project: str, stage_id: str) -> list[dict[str, JsonValue]]:
    """Find only the open exceptions that stop this branch from dispatching."""
    import project_storage
    project = project_storage.normalize(project)
    with stage_store.connection(project) as conn:
        return blocking_in_connection(conn, stage_id)


def final_gate_in_connection(conn: sqlite3.Connection, project: str, goal_id: str) -> FinalReview:
    """Read final acceptance inside the caller's transaction, with no write or model call."""
    contract = records.contract(conn, goal_id)
    required = contract['revision'] > 0 and contract['review_policy'] == 'final'
    pending = ''
    if records.has_table(conn, 'human_decisions'):
        raw = conn.execute("SELECT id FROM human_decisions WHERE goal_id=? AND kind='final' AND status='pending' "
                           'ORDER BY created_at DESC LIMIT 1', (goal_id,)).fetchone()
        pending = raw['id'] if raw else ''
    stages = decode_stages(conn.execute('SELECT * FROM stages WHERE goal_id=?', (goal_id,)).fetchall())
    exceptions_open = any(blocking_in_connection(conn, row['id']) for row in stages if not is_superseded(row))
    approved = (not required or approved_final(conn, project, goal_id)) and not exceptions_open
    return {'required': required, 'approved': approved, 'pending_decision_id': pending}


def final_acceptance_ready(project: str, goal_id: str) -> FinalReview:
    """Report the human final-review requirement to the brain and panel."""
    project = records.owner(project, goal_id)
    with stage_store.connection(project) as conn:
        return final_gate_in_connection(conn, project, goal_id)


def check_completion(project: str, goal_id: str) -> FinalReview:
    """Reject self-approval when the human configured a final-deliverable gate."""
    gate = final_acceptance_ready(project, goal_id)
    if not gate['approved']:
        raise PlanError('human_final_required', '此目标需要人类审核最终产物，主脑不能自行标记完成')
    return gate


def request_final_review(project: str, goal_id: str,
                         evidence_refs: list[str] | None = None, *,
                         effect_scope: HostRef | None = None) -> dict[str, JsonValue]:
    """Submit final evidence only after all live stages passed independent acceptance."""
    project = records.owner(project, goal_id)
    with stage_store.connection(project) as conn:
        stages = decode_stages(conn.execute('SELECT * FROM stages WHERE goal_id=?', (goal_id,)).fetchall())
        live = [row for row in stages if not is_superseded(row)]
        if not live or any(row['status'] not in stage_store.ACCEPTED_STATES for row in live):
            raise PlanError('acceptance_incomplete', '请先完成全部有效阶段的独立验收，再提交最终产物审核')
    payload = DecisionRequest.model_validate({
        'question': '是否接受该目标的最终产物并允许完成？',
        'reason': '全部有效阶段已独立验收通过；请核对固定准则及所附证据。',
        'options': ['接受最终产物', '要求修改'], 'approval_option': '接受最终产物',
        'recommendation': '接受最终产物',
        'evidence_refs': evidence_refs or [], 'affected_stage_ids': [], 'kind': 'final'})
    effect = goal_effects.prepare(effect_scope, 'final-review', 'goal_question', payload.model_dump())
    return create_decision(project, goal_id, payload, effect=effect)
