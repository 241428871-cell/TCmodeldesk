"""AI-owned, versioned task decomposition beneath immutable human criteria.

No call here starts a service or a model. All authority and stage changes live
atomically in the owning project's existing records database.
"""
from __future__ import annotations

from contextlib import nullcontext
import json
import sqlite3
from typing import TYPE_CHECKING

from pydantic import JsonValue

import stage_store
import continuation_store
import goal_file_scope
import goal_effects
from plan_contracts import GoalContract, PlanError, PlanPatch, PlanView
import plan_records as records
from plan_validation import decode_stages, normalize

if TYPE_CHECKING:
    from model_desk_control.host_effects import EffectRequest


def configure_goal(project: str, goal_id: str, payload: GoalContract | dict[str, JsonValue],
                   actor: str = 'human') -> PlanView:
    """Only the trusted human route may establish or change acceptance authority."""
    if actor != 'human':
        raise PlanError('human_only', '验收准则和文件授权只能由人类修改')
    parsed = GoalContract.model_validate(payload)
    project = records.owner(project, goal_id)
    with continuation_store.locked(project, goal_id), stage_store.connection(project) as conn, stage_store._transaction(conn):
        records.ensure_schema(conn)
        previous = records.contract(conn, goal_id)
        goal = conn.execute('SELECT status FROM goals WHERE id=?', (goal_id,)).fetchone()
        if goal['status'] != stage_store.GOAL_ACTIVE:
            raise PlanError('goal_inactive', '已完成或取消的目标不能改写准则；请创建新目标')
        if parsed.base_revision is not None and parsed.base_revision != previous['revision']:
            raise PlanError('criteria_conflict', '准则已更新，请重新读取后再保存')
        existing = decode_stages(conn.execute('SELECT * FROM stages WHERE goal_id=?', (goal_id,)).fetchall())
        allowed = parsed.allowed_files
        scope = goal_file_scope.resolve_scope(parsed.file_scope,
            supplied_files=allowed is not None,
            previous=previous['file_scope'] if previous['revision'] > 0 or existing else None)
        if allowed is None:
            allowed = previous['allowed_files'] if previous['revision'] > 0 else list(dict.fromkeys(
                path for row in existing for path in row['files']))
        if scope == 'project':
            allowed = []
        else:
            allowed = goal_file_scope.validate_files(project, scope, allowed, records.clean_files(allowed))
        review_policy = (previous['review_policy'] if previous['revision'] > 0 and
            'review_policy' not in parsed.model_fields_set else parsed.review_policy)
        value = {**parsed.model_dump(exclude={'base_revision'}), 'allowed_files': allowed,
                 'file_scope': scope, 'review_policy': review_policy}
        conn.execute('INSERT INTO goal_contracts VALUES(?,?,?,?) '
                     'ON CONFLICT(goal_id) DO UPDATE SET revision=excluded.revision, '
                     'payload=excluded.payload,updated_at=excluded.updated_at',
                     (goal_id, previous['revision'] + 1, records.dump(value), records.now()))
        records.log(conn, goal_id, 'human_criteria', actor, records.dump(value))
    return describe_goal(project, goal_id)


def describe_goal(project: str, goal_id: str) -> PlanView:
    """Read criteria, current stages and every revision without starting work."""
    import human_decisions
    project = records.owner(project, goal_id)
    with stage_store.connection(project) as conn:
        contract = records.contract(conn, goal_id)
        stages = decode_stages(conn.execute('SELECT * FROM stages WHERE goal_id=? ORDER BY ordinal,id',
                                            (goal_id,)).fetchall())
        history: list[dict[str, JsonValue]] = []
        if records.has_table(conn, 'plan_revisions'):
            for raw in conn.execute('SELECT * FROM plan_revisions WHERE goal_id=? ORDER BY revision', (goal_id,)):
                row = dict(raw)
                row['patch'] = json.loads(row['patch'])
                row['changes'] = json.loads(row['changes'])
                history.append(row)
        return {'goal_id': goal_id, 'criteria': contract['criteria'],
                'review_policy': contract['review_policy'], 'allowed_files': contract['allowed_files'],
                'file_scope': contract['file_scope'],
                'plan_revision': records.revision(conn, goal_id),
                'contract_revision': contract['revision'], 'history': history, 'stages': stages,
                'final_review': human_decisions.final_gate_in_connection(conn, project, goal_id)}


def revise_plan(project: str, goal_id: str, payload: PlanPatch | dict[str, JsonValue],
                actor: str = 'brain', *, effect: EffectRequest | None = None) -> PlanView:
    """Apply a complete AI plan snapshot or reject it without partial writes."""
    if actor not in {'brain', 'codex', 'dsh'}:
        raise PlanError('brain_only', '任务拆分与阶段修订应由主脑提交')
    parsed = PlanPatch.model_validate(payload)
    project = records.owner(project, goal_id)
    goal_effects.verify(effect, project, goal_id, 'goal_plan', parsed.model_dump())
    import continuation_clock
    authority = continuation_clock.authority_lock(project, goal_id, parsed.authority_epoch) if parsed.authority_epoch is not None else nullcontext()
    created: list[str] = []
    with goal_effects.authority(effect), authority, stage_store.connection(project) as conn, stage_store._transaction(conn):
        records.ensure_schema(conn)
        replay = goal_effects.existing(conn, effect)
        if replay is not None:
            if replay.subject_id != goal_id:
                raise PlanError('effect_subject', '计划回执目标不一致，未重复应用')
            if replay.revision != records.revision(conn, goal_id):
                raise PlanError('effect_plan_revision_changed', '原计划后已有新的修订，不能沿用旧批次继续派发')
            created = [row['id'] for row in conn.execute('SELECT id FROM stages WHERE goal_id=?', (goal_id,))]
        else:
            contract = records.contract(conn, goal_id)
            if not contract['criteria']:
                raise PlanError('criteria_missing', '请人类先固定验收准则，再由 AI 拆分阶段')
            state = records.authority(project, goal_id, conn)
            if state['stopped']:
                raise PlanError('authority_stopped', '人类已停止目标，旧轮次不能再修订计划')
            if parsed.authority_epoch is not None and parsed.authority_epoch != state['authority_epoch']:
                raise PlanError('stale_epoch', '续跑授权已变化，拒绝旧轮次计划')
            revision = records.revision(conn, goal_id)
            if revision != parsed.base_revision:
                raise PlanError('revision_conflict', '计划版本已变化，请重新读取后修订')
            goal = conn.execute('SELECT status FROM goals WHERE id=?', (goal_id,)).fetchone()
            if goal['status'] != stage_store.GOAL_ACTIVE:
                raise PlanError('goal_inactive', '已完成或取消的目标不能继续修订')
            existing = decode_stages(conn.execute('SELECT * FROM stages WHERE goal_id=?', (goal_id,)).fetchall())
            normalized = normalize(parsed.stages, existing, contract['allowed_files'], revision + 1,
                project=project, file_scope=contract['file_scope'])
            _write_plan(conn, project, goal_id, normalized['stages'], existing, normalized['superseded'], revision + 1)
            changes = {key: value for key, value in normalized.items() if key != 'stages'}
            changes['stage_ids'] = [row['id'] for row in normalized['stages']]
            conn.execute('INSERT INTO plan_revisions VALUES(?,?,?,?,?,?,?)',
                         (goal_id, revision + 1, parsed.reason, actor, records.dump(parsed.model_dump()),
                          records.dump(changes), records.now()))
            records.log(conn, goal_id, 'plan_revision', actor,
                        records.dump({'revision': revision + 1, 'reason': parsed.reason, **changes}))
            created = normalized['added']
            goal_effects.save(conn, effect, goal_id, revision + 1)
    for stage_id in created:
        stage_store._register_stage(stage_id, project)
    return describe_goal(project, goal_id)


def _write_plan(conn: sqlite3.Connection, project: str, goal_id: str,
                stages: list[dict[str, JsonValue]], existing: list[dict[str, JsonValue]],
                superseded: list[str], revision: int) -> None:
    """Preserve execution metadata while storing mutable steps and retired history."""
    prior = {row['id']: row for row in existing}
    ordinal = max((row['ordinal'] for row in existing), default=0)
    for row in stages:
        previous = prior.get(row['id'])
        data = previous['data'] if previous else {}
        data = {**data, 'dynamic_plan': {'key': row['key'], 'revision': revision}}
        values = (row['title'], row['objective'], row['kind'], records.dump(row['files']),
                  records.dump(row['acceptance']), records.dump(row['candidates']), row['fixed_model'],
                  records.dump(row['depends_on']), row['session_policy'], records.now(), records.dump(data))
        if previous:
            conn.execute('UPDATE stages SET title=?,objective=?,kind=?,files=?,acceptance=?,candidates=?, '
                         'fixed_model=?,depends_on=?,session_policy=?,updated_at=?,data=? WHERE id=?',
                         (*values, row['id']))
        else:
            ordinal += 1
            status = stage_store.PENDING if row['depends_on'] else stage_store.READY
            conn.execute('INSERT INTO stages (title,objective,kind,files,acceptance,candidates,fixed_model, '
                         'depends_on,session_policy,updated_at,data,id,goal_id,ordinal,project,status,created_at) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (*values, row['id'], goal_id, ordinal, project, status, records.now()))
    for stage_id in superseded:
        previous = prior[stage_id]
        data = {**previous['data'], 'dynamic_plan': {**previous['data'].get('dynamic_plan', {}),
                                                  'superseded_by': revision}}
        conn.execute('UPDATE stages SET status=?,data=?,updated_at=? WHERE id=?',
                     (stage_store.CANCELLED, records.dump(data), records.now(), stage_id))
