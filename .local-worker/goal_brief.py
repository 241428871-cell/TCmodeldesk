from __future__ import annotations

from hashlib import sha256
from sqlite3 import Connection
from typing import ClassVar, Final, TypedDict

import continuation_store
import control_bootstrap
import plan_records
from plan_contracts import GoalInput, PlanBrief, PlanError
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

control_bootstrap.bind_source()
from model_desk_control.database import text_cursor


class CloseoutIdentity(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra='ignore', frozen=True)
    id: str


JSON_MAP: Final = TypeAdapter(dict[str, JsonValue])
JSON_LIST: Final = TypeAdapter(list[JsonValue])
CLOSEOUTS: Final = TypeAdapter(list[CloseoutIdentity])

class BriefView(TypedDict):
    brief_input: dict[str, JsonValue]
    brief: dict[str, JsonValue] | None
    completion_pending: list[str]


def original(conn: Connection, goal_id: str, contract: plan_records.ContractRecord) -> GoalInput:
    supplied = contract.get('brief_input')
    if supplied is not None:
        return GoalInput.model_validate(supplied)
    row = text_cursor(conn, "SELECT json_object('title',title,'objective',objective) FROM goals WHERE id=?",
                      (goal_id,)).fetchone()
    if row is None:
        raise PlanError('goal_missing', '当前项目中不存在该目标')
    return GoalInput.model_validate_json(row[0])


def latest(conn: Connection, goal_id: str) -> PlanBrief | None:
    if plan_records.has_table(conn, 'plan_revisions'):
        for row in text_cursor(conn, 'SELECT patch FROM plan_revisions WHERE goal_id=? ORDER BY revision DESC',
                               (goal_id,)).fetchall():
            value = JSON_MAP.validate_json(row[0]).get('brief')
            if value is not None:
                return PlanBrief.model_validate(value)
    return None


def describe(conn: Connection, goal_id: str, contract: plan_records.ContractRecord) -> BriefView:
    supplied, completed = original(conn, goal_id, contract), latest(conn, goal_id)
    missing = ([name for name in ('title', 'objective')
                if not getattr(supplied, name) and not (completed and getattr(completed, name))]
               if contract.get('brief_input') is not None else [])
    if not contract['criteria'] and not (completed and completed.criteria):
        missing.append('criteria')
    clock = continuation_store.read(conn, goal_id)
    if (contract.get('brief_input') is not None and completed is None
            and not (clock or {}).get('obligations')):
        missing.append('obligations')
    return {'brief_input': supplied.model_dump(), 'brief': completed.model_dump() if completed else None,
            'completion_pending': missing}


def apply(conn: Connection, project: str, goal_id: str,
          contract: plan_records.ContractRecord, proposal: PlanBrief | None) -> PlanBrief | None:
    state = describe(conn, goal_id, contract)
    if proposal is None:
        if state['completion_pending']:
            raise PlanError('brief_missing', '请主脑先按已有需求补充缺失内容，再提交计划')
        return None
    supplied, previous = original(conn, goal_id, contract), latest(conn, goal_id)
    for name in ('title', 'objective'):
        if getattr(supplied, name) and getattr(proposal, name) not in ('', getattr(supplied, name)):
            raise PlanError('human_brief', '主脑不能改写人类已填写的目标名称或期望结果')
    prior = previous or PlanBrief()
    completed = PlanBrief(
        title='' if supplied.title else proposal.title.strip() or prior.title,
        objective='' if supplied.objective else proposal.objective.strip() or prior.objective,
        criteria=[value for value in (proposal.criteria or prior.criteria) if value not in contract['criteria']],
        obligations=list(dict.fromkeys([*prior.obligations, *proposal.obligations])))
    if (not (supplied.title or completed.title) or not (supplied.objective or completed.objective)
            or not (contract['criteria'] or completed.criteria)):
        raise PlanError('brief_incomplete', '主脑补充后仍需有目标名称、期望结果和可核验的验收准则')
    _ = conn.execute('UPDATE goals SET title=?,objective=?,updated_at=? WHERE id=?',
                 (supplied.title or completed.title, supplied.objective or completed.objective,
                  plan_records.now(), goal_id))
    if completed.obligations:
        continuation_store.schema(conn)
        clock = continuation_store.read(conn, goal_id) or continuation_store.new_clock(project, goal_id, 1800)
        rows = JSON_LIST.validate_python(clock['obligations'])
        known = {item.id for item in CLOSEOUTS.validate_python(rows)}
        for title in completed.obligations:
            ident = 'ai-' + sha256(title.encode()).hexdigest()[:20]
            if ident not in known:
                rows.append({'id': ident, 'title': title, 'status': 'pending',
                             'evidence': '', 'source': 'brain'})
                known.add(ident)
        clock['obligations'] = rows
        continuation_store.write(conn, clock)
    return completed
