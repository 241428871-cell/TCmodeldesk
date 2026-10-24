"""Project-local schema and fingerprints shared by planning and human review."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
import sqlite3
from typing import Literal, TypedDict

from pydantic import JsonValue

import model_registry as registry
import project_storage
import stage_store
from plan_contracts import PlanError


class ContractRecord(TypedDict):
    criteria: list[str]
    review_policy: str
    allowed_files: list[str]
    file_scope: Literal['project', 'selected']
    revision: int


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create only on explicit governance writes; never commit a caller's transaction."""
    conn.execute('CREATE TABLE IF NOT EXISTS goal_contracts ('
                 'goal_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL, '
                 'updated_at TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS plan_revisions ('
                 'goal_id TEXT NOT NULL, revision INTEGER NOT NULL, reason TEXT NOT NULL, '
                 'actor TEXT NOT NULL, patch TEXT NOT NULL, changes TEXT NOT NULL, created_at TEXT NOT NULL, '
                 'PRIMARY KEY(goal_id, revision))')
    conn.execute('CREATE TABLE IF NOT EXISTS human_decisions ('
                 'id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, kind TEXT NOT NULL, '
                 'status TEXT NOT NULL, subject_hash TEXT NOT NULL, fingerprint TEXT NOT NULL, '
                 'payload TEXT NOT NULL, answer TEXT NOT NULL DEFAULT \'\', notes TEXT NOT NULL DEFAULT \'\', '
                 'created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS human_decisions_goal '
                 'ON human_decisions(goal_id,status)')


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    """Schema discovery allows old project reads without a migration write."""
    return conn.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?',
                        (name,)).fetchone() is not None


def dump(value: JsonValue) -> str:
    """Canonical JSON makes hashes and stored patches reproducible."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value: JsonValue) -> str:
    """Hash bounded metadata, never secret provider configuration."""
    return hashlib.sha256(dump(value).encode('utf-8')).hexdigest()


def owner(project: str, goal_id: str) -> str:
    """Governance never falls back to a different project's or legacy records."""
    project = project_storage.normalize(project)
    with stage_store.connection(project) as conn:
        row = conn.execute('SELECT project FROM goals WHERE id=?', (goal_id,)).fetchone()
        if row is None:
            raise PlanError('goal_missing', '当前项目中不存在该目标；请先创建项目目标')
        if not row['project'] or project_storage.normalize(row['project']) != project:
            raise PlanError('project_mismatch', '目标与当前项目不一致')
    return project


def contract(conn: sqlite3.Connection, goal_id: str) -> ContractRecord:
    """An unconfigured goal is readable but cannot be revised by AI yet."""
    if has_table(conn, 'goal_contracts'):
        row = conn.execute('SELECT * FROM goal_contracts WHERE goal_id=?', (goal_id,)).fetchone()
        if row is not None:
            value = json.loads(row['payload'])
            file_scope = value.get('file_scope') or 'selected'
            if file_scope not in {'project', 'selected'}:
                raise PlanError('file_scope', '目标的文件授权类型无效，未扩大权限')
            return {**value, 'file_scope': file_scope, 'revision': row['revision']}
    return {'criteria': [], 'review_policy': 'exceptions', 'allowed_files': [],
            'file_scope': 'selected', 'revision': 0}


def revision(conn: sqlite3.Connection, goal_id: str) -> int:
    """Read the committed plan generation without creating governance records."""
    if not has_table(conn, 'plan_revisions'):
        return 0
    row = conn.execute('SELECT COALESCE(MAX(revision),0) AS revision FROM plan_revisions '
                       'WHERE goal_id=?', (goal_id,)).fetchone()
    return int(row['revision'])


def clean_files(values: list[str]) -> list[str]:
    """Keep the existing project-relative, finite file authorization boundary."""
    clean: list[str] = []
    for value in values:
        path = PurePosixPath(value.replace('\\', '/'))
        if not value.strip() or path.is_absolute() or '..' in path.parts or ':' in value:
            raise PlanError('file_scope', '文件必须是项目内的明确相对路径')
        text = str(path)
        if text == '.' or '*' in text or '?' in text or len(text) > 500:
            raise PlanError('file_scope', '修改授权必须列出明确文件，不能使用目录或通配符')
        if text not in clean:
            clean.append(text)
    if len(clean) > 200:
        raise PlanError('file_scope', '单个目标的文件范围最多 200 项')
    return clean


def authority(project: str, goal_id: str, conn: sqlite3.Connection | None = None) -> dict[str, JsonValue]:
    """Bind proposals to the continuation authority; optional clock stays passive."""
    import continuation_clock
    import engineering_policy
    import continuation_store
    state = (continuation_store.read(conn, goal_id) or {'authority_epoch': 0, 'user_intent': 'RUN'}) if conn is not None else continuation_clock.snapshot(project, goal_id)
    policy = engineering_policy.policy_snapshot(project)
    return {'authority_epoch': int(state['authority_epoch']),
            'policy_revision': policy['revision'], 'policy_snapshot_id': policy['snapshot_id'],
            'stopped': state['user_intent'] == 'STOP'}


def fingerprint(conn: sqlite3.Connection, project: str, goal_id: str,
                artifact_digest: str = '') -> dict[str, JsonValue]:
    """Review authority covers goal criteria, current plan, policy, stop epoch and artifact."""
    current = contract(conn, goal_id)
    return {**authority(project, goal_id, conn), 'plan_revision': revision(conn, goal_id),
            'contract_revision': current['revision'], 'artifact_digest': artifact_digest,
            'criteria_digest': digest(current['criteria'])}


def log(conn: sqlite3.Connection, goal_id: str, action: str, actor: str, detail: str) -> None:
    """Use the existing per-project audit stream rather than a second global history."""
    stage_store._log(conn, action=action, actor=actor, goal_id=goal_id, detail=detail)


def now() -> str:
    """Use the same timestamp format as stages and task receipts."""
    return registry.now()
