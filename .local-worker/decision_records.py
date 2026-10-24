"""Evidence-bound human review records and dependent-stage blocking."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from pydantic import JsonValue

from plan_contracts import PlanError
import plan_records as records
import goal_file_scope
from plan_validation import FIELDS, decode_stages, is_superseded


def artifact_digest(conn: sqlite3.Connection, project: str, goal_id: str) -> str:
    """Bind acceptance to stage evidence and actual authorized deliverable bytes."""
    stages = decode_stages(conn.execute('SELECT * FROM stages WHERE goal_id=? ORDER BY id', (goal_id,)).fetchall())
    evidence: list[JsonValue] = [
        {'id': row['id'], 'status': row['status'], 'task_ids': row['task_ids'],
         'updated_at': row['updated_at'], 'acceptance': row['acceptance']}
        for row in stages if not is_superseded(row)]
    root = Path(project).resolve()
    contract = records.contract(conn, goal_id)
    files = (list(dict.fromkeys(path for row in stages if not is_superseded(row) for path in row['files']))
             if contract['file_scope'] == 'project' else contract['allowed_files'])
    for relative in files:
        goal_file_scope.validate_files(project, 'project', [], [relative])
        path = (root / relative).resolve()
        value = 'missing'
        if path.is_relative_to(root) and path.is_file():
            if path.stat().st_size > 20 * 1024 * 1024:
                raise PlanError('artifact_size', '审核文件超过 20 MB，请提交较小的独立验收产物')
            value = hashlib.sha256(path.read_bytes()).hexdigest()
        if not path.is_relative_to(root):
            raise PlanError('artifact_scope', '审核文件已通过链接移出项目范围，拒绝读取')
        evidence.append({'file': relative, 'sha256': value})
    return records.digest(evidence)


def current_fingerprint(conn: sqlite3.Connection, project: str, goal_id: str) -> dict[str, JsonValue]:
    """Current subject generation includes artifact content, criteria and authority."""
    return records.fingerprint(conn, project, goal_id, artifact_digest(conn, project, goal_id))


def decision_row(row: sqlite3.Row) -> dict[str, JsonValue]:
    """Return a browser-safe record with an opaque subject hash and explicit options."""
    payload = json.loads(row['payload'])
    return {**dict(row), **payload, 'fingerprint': json.loads(row['fingerprint'])}


def blocking_in_connection(conn: sqlite3.Connection, stage_id: str) -> list[dict[str, JsonValue]]:
    """Only the explicitly affected branch waits; final review never blocks execution."""
    if not records.has_table(conn, 'human_decisions'):
        return []
    stage = conn.execute('SELECT goal_id FROM stages WHERE id=?', (stage_id,)).fetchone()
    if stage is None:
        return []
    rows = conn.execute("SELECT * FROM human_decisions WHERE goal_id=? AND kind='exception' AND status IN ('pending','answered')",
                        (stage['goal_id'],))
    blocked: list[dict[str, JsonValue]] = []
    current = decode_stages(conn.execute('SELECT * FROM stages WHERE id=?', (stage_id,)).fetchall())[0]
    stage_digest = records.digest({field: current[field] for field in FIELDS})
    for raw in rows:
        value = decision_row(raw)
        denied = value['status'] == 'answered' and (not has_explicit_approval(value) or value['answer'] != value['approval_option_id'])
        original_digest = value.get('affected_stage_digests', {}).get(stage_id, stage_digest)
        must_replan = denied and original_digest == stage_digest
        if stage_id in value['affected_stage_ids'] and (value['status'] == 'pending' or must_replan):
            blocked.append({**value, 'requires_replan': must_replan})
    return blocked


def approved_final(conn: sqlite3.Connection, project: str, goal_id: str) -> bool:
    """A final answer expires after plan, criteria, policy, epoch or artifact changes."""
    if not records.has_table(conn, 'human_decisions'):
        return False
    rows = conn.execute("SELECT * FROM human_decisions WHERE goal_id=? AND kind='final' AND status='answered' "
                        'ORDER BY created_at DESC', (goal_id,))
    current = current_fingerprint(conn, project, goal_id)
    for raw in rows:
        row = decision_row(raw)
        if has_explicit_approval(row) and row['fingerprint'] == current and row['answer'] == row['approval_option_id']:
            return True
    return False


def has_explicit_approval(value: dict[str, JsonValue]) -> bool:
    """Historical implicit ordering never grants permission to continue or complete."""
    declared = value.get('approval_option')
    return bool(declared) and sum(1 for option in value['options']
                                  if option['label'] == declared and option['id'] == value['approval_option_id']) == 1
