"""Project-scoped permanent history deletion with preview, dependency guards and crash recovery."""
from contextlib import contextmanager, ExitStack
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
import sqlite3
import time

import budget_relay
import control_bootstrap
import lease_guard
import model_registry as registry
import project_scope as scope
import project_storage
import project_views
import stage_store

LOCK = threading.RLock()
PREVIEWS = {}
TERMINAL = {'completed', 'failed', 'cancelled'}
ID = re.compile(r'[A-Za-z0-9_-]{1,80}')


def serialized(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        with LOCK:
            return function(*args, **kwargs)
    return wrapper


def require_project(view):
    view = scope.parse_view(view)
    if view['mode'] != 'project':
        raise ValueError('请先选择具体项目，再管理任务记录')
    return view


def records(view=None):
    """Live task records, optionally limited to one explicit project's store.

    A project request enumerates the real directories itself, so the global
    locator is not a precondition for listing or cleaning that project's history.
    """
    out = {}
    for entry in project_storage.iter_task_dirs(view):
        folder = entry['run']
        if not ID.fullmatch(folder.name):
            continue
        file = folder / 'task.json'
        if not file.exists():
            if (folder / 'result.json').exists():
                row = registry.read_json(folder / 'result.json')
                out[folder.name] = {'id':folder.name, 'project':row.get('project'),
                    'status':'completed' if row.get('exit_code') == 0 else 'failed',
                    'title':'旧版任务 '+folder.name, '_legacy':True}
            continue
        row = registry.read_task(folder)
        if not isinstance(row, dict) or row.get('id', folder.name) != folder.name:
            raise ValueError('任务记录不可读或身份不一致，无法安全清理')
        out[folder.name] = {**row, 'id':folder.name}
    return out


def record_roots(view=None):
    """Live task id -> the runs root that owns it (project store or legacy)."""
    return {entry['task_id']: entry['root'] for entry in project_storage.iter_task_dirs(view)}


def analysis_rows(view=None):
    """Every live record for the safety analysis, plus the view's own records.

    The extra union keeps a project request readable when the global locator is
    missing: a record physically inside the requested project is still analysed
    even though the aggregate enumeration cannot see that store without an index.
    """
    rows = records()
    if view is not None:
        for key, row in records(view).items():
            rows.setdefault(key, row)
    return rows


def _connections(project):
    """Open every store that can hold this project's goals/stages.

    A project keeps pre-migration rows in the legacy global DB and newer rows in
    its own records.db; deletion/listing must see both without duplicating ids.
    """
    stack = ExitStack()
    try:
        conns = [stack.enter_context(stage_store.connection(project)),
                 stack.enter_context(stage_store.connection())]
    except Exception:
        stack.close()
        raise
    return stack, conns


def database_rows_multi(conns):
    goals, stages = {}, {}
    for conn in conns:
        conn_goals, conn_stages = database_rows(conn)
        for key, value in conn_goals.items():
            goals.setdefault(key, value)
        for key, value in conn_stages.items():
            stages.setdefault(key, value)
    return goals, stages


def database_rows(conn):
    goals = {row['id']:dict(row) for row in conn.execute('SELECT * FROM goals')}
    stages = {row['id']:stage_store._row_stage(row) for row in conn.execute('SELECT * FROM stages')}
    return goals, stages


def references(row):
    result = set()
    for key in ('continue_task','continued_from','source_task','source_task_id','rework_of','parent_task_id'):
        if isinstance(row.get(key), str) and row[key]: result.add(row[key])
    for nested in ('delegation','route'):
        if isinstance(row.get(nested),dict): result.update(references(row[nested]))
    return result


def stage_links(rows, stages):
    links = {key:set() for key in rows}
    for stage in stages.values():
        for key in [*stage['task_ids'], stage.get('active_task_id')]:
            if key in links: links[key].add(stage['goal_id'])
    for key, row in rows.items():
        if row.get('goal_id'): links[key].add(row['goal_id'])
        if row.get('stage_id'):
            stage = stages.get(row['stage_id'])
            links[key].add(stage['goal_id'] if stage else '__missing_goal__')
    return links


def safe_directory(path, parent):
    path, parent = Path(path), Path(parent).resolve()
    if path.is_symlink() or os.path.isjunction(path) or not path.resolve().is_relative_to(parent) or path.resolve() == parent:
        raise ValueError('记录目录为外部链接或越界路径，拒绝删除')
    # Do not follow a reparse point anywhere inside a run, even during cleanup.
    def visit(directory):
        for entry in os.scandir(directory):
            if entry.is_symlink() or os.path.isjunction(entry.path):
                raise ValueError('记录中包含链接目录，拒绝递归删除')
            if entry.is_dir(follow_symlinks=False): visit(entry.path)
    if path.exists(): visit(path)
    return path


def candidate_reason(key, rows, links, goals, all_projects, roots=None):
    row = rows[key]
    resource_reason = lease_guard.reason(row)
    if resource_reason:
        return resource_reason
    if row.get('status') not in TERMINAL:
        return '运行中、排队或状态未知的任务不能删除'
    cost = budget_relay.task_cost_state(row)
    if cost['unsettled_cny'] > 0 or cost['unknown_calls'] > 0:
        return '费用尚未结算或存在结果未知的调用，需先核对后再删除'
    for goal_id in links[key]:
        goal = goals.get(goal_id)
        if not goal or goal['status'] not in {'completed','cancelled'}:
            return '关联目标仍在进行或身份缺失，需保留阶段交接'
        if scope.project_key(goal['project']) != scope.project_key(row.get('project')):
            return '关联目标项目不一致，拒绝删除'
    # The owning runs root comes from the real enumeration (or the locator when
    # no scope was supplied), so the path guard never has to guess.
    root = (roots or {}).get(key) or project_storage.owning_root(key)
    run = Path(root) / key
    try:
        safe_directory(run, root)
        for project in all_projects:
            if Path(project).resolve().is_relative_to(run.resolve()):
                return '记录目录包含项目文件，不能整体删除'
    except (ValueError,OSError) as exc:
        return str(exc)
    return ''


def goal_brain_root(project):
    return Path(project_storage.normalize(project))/project_storage.DESK_DIR/'goal-brain'


def goal_brain_dir(project, goal_id):
    if not ID.fullmatch(goal_id):raise ValueError('目标标识无效，拒绝清理')
    return goal_brain_root(project)/goal_id


def _inventories(project, goal_ids, task_ids, conns):
    """Read-only protection inventory; any live or unresolved record blocks the plan."""
    control_bootstrap.bind_source()
    from model_desk_control.host_purge import PurgeScope, inventory
    purge_scope=PurgeScope(project=project_storage.normalize(project),
                           goal_ids=tuple(sorted(goal_ids)),task_ids=tuple(sorted(task_ids)))
    found=[inventory(conn,purge_scope) for conn in conns]
    for item in found:
        if item.blockers:raise ValueError('「保护记录」：'+item.blockers[0])
    return purge_scope,found


def plan(view, requested, conn, _snapshot=None, _fingerprint=True):
    view = require_project(view)
    if not isinstance(requested,list) or not requested or len(requested)>2000 or any(not isinstance(key,str) or not ID.fullmatch(key) for key in requested):
        raise ValueError('请选择有效任务记录，单次最多2000条')
    roots=record_roots(view)
    if _snapshot is None:
        # Preview/commit always analyse the *global* record set for surviving
        # references and nested project folders: a cross-project reference or a
        # project directory living inside a run makes the deletion unsafe even
        # when the target itself is inside the current view.  The view's own
        # records are added so a missing locator cannot hide a real target.
        rows=analysis_rows(view);goals,stages=database_rows_multi(conn)
    else:rows,goals,stages=_snapshot
    links = stage_links(rows,stages)
    selected=set(requested)
    for key in selected:
        if key not in rows or not scope.belongs_to_project(rows[key],view):
            raise ValueError('任务不存在或不属于当前项目')
    # A finished goal's handovers/attempts are one dependency group. Never silently
    # delete just its accepted prerequisite while leaving downstream stages behind.
    chosen_goals=set()
    while True:
        before=len(selected)
        chosen_goals.update(g for key in selected for g in links[key])
        for key,row in rows.items():
            if links[key] & chosen_goals: selected.add(key)
            elif references(row) & selected: selected.add(key)
        if len(selected)==before: break
    projects={str(Path(project).resolve()) for project in
              {row.get('project') or row.get('path') for row in
               [*rows.values(),*goals.values(),*project_views.project_bookmarks()]
               if row.get('project') or row.get('path')}}
    for key in selected:
        if not scope.belongs_to_project(rows[key],view):
            raise ValueError('关联记录跨项目，不能联动删除')
        reason=candidate_reason(key,rows,links,goals,projects,roots)
        if reason: raise ValueError(f'「{rows[key].get("title",key)}」：{reason}')
    for goal_id in chosen_goals:
        goal=goals.get(goal_id)
        if not goal or goal['status'] not in {'completed','cancelled'}: raise ValueError('关联目标尚未结束')
        if not scope.belongs_to_project(goal,view): raise ValueError('关联目标跨项目')
    chosen_stages=[row for row in stages.values() if row['goal_id'] in chosen_goals]
    if any(row['status'] in {'dispatched','running','recovery_pending'} for row in chosen_stages):
        raise ValueError('关联阶段尚未停止或待恢复，不能删除')
    # Every durable goal/task record in each store: rounds, receipts, queue, initialization
    # manifests (private prompt copies), decisions, events and stage branches.
    purge_scope,inventories=_inventories(view['project'],chosen_goals,selected,conn)
    goal_dirs=[goal for goal in sorted(chosen_goals) if goal_brain_dir(view['project'],goal).exists()]
    for goal in goal_dirs:safe_directory(goal_brain_dir(view['project'],goal),goal_brain_root(view['project']))
    # Preview fingerprint includes all reference/status metadata and targeted files.
    digest=hashlib.sha256()
    if _fingerprint:
        digest.update(json.dumps({'rows':rows,'goals':goals,'stages':stages},sort_keys=True,ensure_ascii=False).encode())
        digest.update(json.dumps([item.digest for item in inventories]).encode())
        targets=[(key,Path(roots.get(key) or project_storage.owning_root(key))/key) for key in sorted(selected)]
        targets+=[('goal-brain/'+goal,goal_brain_dir(view['project'],goal)) for goal in goal_dirs]
        for key,run in targets:
            for path in sorted(run.rglob('*')):
                if path.is_file():
                    info=path.stat();digest.update(f'{key}/{path.relative_to(run)}:{info.st_size}:{info.st_mtime_ns}'.encode())
    counts={}
    for item in inventories:
        for table,count in item.counts:counts[table]=counts.get(table,0)+count
    return {'project':view['project'],'tasks':[{'id':key,'title':rows[key].get('title',key),'status':rows[key]['status']} for key in sorted(selected)],
            'requested_ids':sorted(set(requested)),'added_ids':sorted(selected-set(requested)),
            'goals':[{'id':key,'title':goals[key]['title']} for key in sorted(chosen_goals)],
            'stage_ids':sorted(row['id'] for row in chosen_stages),'stage_count':len(chosen_stages),'_fingerprint':digest.hexdigest(),
            'records':{table:count for table,count in sorted(counts.items()) if count},
            'goal_brain_rounds':len(goal_dirs),
            '_scope':purge_scope,'_inventories':inventories,'_goal_dirs':goal_dirs,
            'warnings':['永久删除工作台记录、提示词、结果、日志、验收和交接，无法撤销',
                        '同时删除主脑轮次与原生输出、初始化私有提示词副本、执行队列、审核与授权使用、事件、阶段分支和图检查点',
                        '关联的已结束目标/阶段及后续引用记录将一并删除，请检查最终清单',
                        '保留项目文件、模型配置和已发生费用总账；DSH自身保存的会话不在此次清理范围']}


@serialized
def listing(view):
    view=require_project(view)
    stack,conns=_connections(view['project'])
    try:
        # Displayed rows come from the project's own directories; the safety
        # analysis still sees every live record, so the ``deletable`` hint can
        # never claim a record is safe when a cross-project reference says not.
        rows=records(view);safety=analysis_rows(view);goals,stages=database_rows_multi(conns)
        result=[]
        for key,row in rows.items():
            try:
                evaluated=plan(view,[key],conns,_snapshot=(safety,goals,stages),_fingerprint=False); reason=''
                group_title='、'.join(g['title'] for g in evaluated['goals'])
            except ValueError as exc:reason=str(exc);group_title=''
            result.append({k:row.get(k,'') for k in ('id','title','status','created_at','ended_at','model_name','goal_id')}
                          | {'deletable':not reason,'blocked_reason':reason,'group_title':group_title})
    finally:
        stack.close()
    result.sort(key=lambda row:row['created_at'],reverse=True)
    return {'project':view['project'],'tasks':result,'total':len(result)}


@serialized
def preview(view, ids):
    view=require_project(view)
    stack,conns=_connections(view['project'])
    try:
        result=plan(view,ids,conns)
    finally:
        stack.close()
    now=time.monotonic()
    for key in list(PREVIEWS):
        if now-PREVIEWS[key]['_created']>300: PREVIEWS.pop(key)
    token=secrets.token_urlsafe(32)
    PREVIEWS[token]={**result,'_created':now}
    return {k:v for k,v in result.items() if not k.startswith('_') and k!='stage_ids'} | {'token':token,'expires_in':300}


def staging_root():
    root=Path(registry.HERE)/'task-deletions'
    if root.is_symlink() or os.path.isjunction(root):raise ValueError('清理暂存目录为链接，拒绝使用')
    return root


def journal_schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS task_deletion_log (id TEXT PRIMARY KEY,project TEXT NOT NULL,task_count INTEGER,goal_count INTEGER,stage_count INTEGER,phase TEXT NOT NULL,at TEXT NOT NULL)')


def purge(directory):
    safe_directory(directory,staging_root())
    shutil.rmtree(directory)


def restore(directory,ids,roots=None,goal_dirs=(),project=None):
    roots = roots if isinstance(roots,dict) else {}
    for key in ids:
        if not isinstance(key,str) or not ID.fullmatch(key):raise ValueError('清理恢复清单无效')
        source=directory/key
        if source.exists():
            safe_directory(source,directory)
            parent=Path(roots[key]) if roots.get(key) else project_storage.owning_root(key)
            destination=parent/key
            if destination.exists():raise ValueError('任务目录已存在，无法恢复清理事务')
            destination.parent.mkdir(parents=True,exist_ok=True)
            os.rename(source,destination)
            owner=registry.project_of_run(destination)
            if owner:
                # The aborted transaction never committed the index cleanup, but
                # re-register defensively so a restored project record is findable.
                try:project_storage.register('task',key,owner)
                except (OSError,ValueError):pass
    for goal in goal_dirs:
        source=directory/'goal-brain'/goal
        if source.exists():
            safe_directory(source,directory)
            destination=goal_brain_dir(project,goal)
            if destination.exists():raise ValueError('主脑轮次目录已存在，无法恢复清理事务')
            destination.parent.mkdir(parents=True,exist_ok=True)
            os.rename(source,destination)
    purge(directory)


def _delete_stage_rows(conn,goal_ids,ids):
    for goal_id in goal_ids:
        conn.execute('DELETE FROM stage_log WHERE goal_id=?',(goal_id,))
        conn.execute('DELETE FROM stages WHERE goal_id=?',(goal_id,))
        conn.execute('DELETE FROM goals WHERE id=?',(goal_id,))
    for key in ids:conn.execute('DELETE FROM stage_log WHERE task_id=?',(key,))


def _secure(conn):
    # Deleted pages are overwritten rather than left readable in the database file.
    conn.execute('PRAGMA secure_delete=ON')


def _flush_wal(conn):
    try:conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    except sqlite3.Error:pass


def _purge_threads(project,manifest):
    """Remove only this job's checkpoint threads; positions hold references, not prompts."""
    root=Path(project)/project_storage.DESK_DIR/'langgraph'
    for name,key in (('checkpoints.sqlite','goal_threads'),('task-checkpoints.sqlite','task_threads')):
        threads=[str(item) for item in manifest.get(key) or []]
        path=root/name
        if not threads or not path.is_file() or path.is_symlink():continue
        conn=sqlite3.connect(str(path),timeout=15,isolation_level=None)
        try:
            _secure(conn)
            tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            conn.execute('BEGIN IMMEDIATE')
            for table in ('checkpoints','writes'):
                if table in tables:
                    for thread in threads:conn.execute(f'DELETE FROM {table} WHERE thread_id=?',(thread,))
            conn.execute('COMMIT')
            _flush_wal(conn)
        finally:
            conn.close()


def _journal(manifest):
    """The store that committed the job's rows also holds its journal entry."""
    if manifest.get('journal')=='project':return stage_store.connection(manifest['project'])
    return stage_store.connection()


def _complete(job_id,manifest,directory):
    """Idempotent work after the commit point; recovery repeats it until purged."""
    ids=[str(key) for key in manifest.get('ids') or []]
    goal_ids=[str(goal) for goal in manifest.get('goal_ids') or []]
    if manifest.get('journal')=='project':
        with stage_store.connection() as conn:
            _secure(conn)
            with stage_store._transaction(conn):_delete_stage_rows(conn,goal_ids,ids)
    for key in ids:project_storage.forget_task(key,manifest.get('project') or None)
    if manifest.get('project'):_purge_threads(manifest['project'],manifest)
    if directory.exists():purge(directory)
    with _journal(manifest) as conn:
        journal_schema(conn)
        conn.execute('UPDATE task_deletion_log SET phase="purged" WHERE id=?',(job_id,))
        _flush_wal(conn)


@serialized
def recover():
    root=staging_root()
    if not root.exists():return
    with stage_store.connection() as conn:
        journal_schema(conn)
        for directory in root.iterdir():
            if not re.fullmatch(r'[0-9a-f]{32}',directory.name):continue
            safe_directory(directory,root)
            manifest=registry.read_json(directory/'manifest.json') if (directory/'manifest.json').exists() else None
            if isinstance(manifest,dict) and manifest.get('version')==2:
                with _journal(manifest) as journal:
                    journal_schema(journal)
                    entry=journal.execute('SELECT phase FROM task_deletion_log WHERE id=?',(directory.name,)).fetchone()
                if entry:_complete(directory.name,manifest,directory)
                else:restore(directory,manifest['ids'],manifest.get('roots'),manifest.get('goal_dirs') or (),manifest.get('project'))
                continue
            entry=conn.execute('SELECT phase FROM task_deletion_log WHERE id=?',(directory.name,)).fetchone()
            if entry:
                purge(directory)
                conn.execute('UPDATE task_deletion_log SET phase="purged" WHERE id=?',(directory.name,))
            elif manifest is None and all(
                    item.is_file() and item.name.startswith('manifest.json.') and item.name.endswith('.tmp')
                    for item in directory.iterdir()):
                # Crash before write_json finished: no run could have moved yet.
                purge(directory)
            else:
                restore(directory,manifest['ids'],manifest.get('roots'))


@serialized
def delete(view,token,idle_guard=None):
    view=require_project(view)
    selected=PREVIEWS.get(token) if isinstance(token,str) else None
    if not selected or time.monotonic()-selected['_created']>300:
        raise ValueError('删除预览已过期，请重新选择并预览')
    if scope.project_key(selected['project'])!=scope.project_key(view['project']):
        raise ValueError('项目已切换，请重新预览删除清单')
    recover()
    control_bootstrap.bind_source()
    from model_desk_control.host_purge import purge as purge_records
    project=project_storage.normalize(view['project'])
    store=project_storage.records_db(project)
    # The store holding this project's rows is the single commit point and holds the journal;
    # legacy-only projects without a records.db keep the global journal.
    durable=store.is_file() and not store.is_symlink()
    stack,conns=_connections(view['project'])
    commit_conn=conns[0] if durable else conns[-1]
    try:
        for conn in conns:_secure(conn)
        journal_schema(commit_conn)
        with stage_store._transaction(commit_conn):
            current=plan(view,selected['requested_ids'],conns)
            if current['_fingerprint']!=selected['_fingerprint']:
                raise ValueError('记录或依赖发生变化，请重新预览')
            if any(row.get('status') not in TERMINAL for row in records().values()):
                raise ValueError('请等待工作台执行队列空闲后再永久删除记录')
            if idle_guard is not None:idle_guard()
            ids=[row['id'] for row in current['tasks']]
            goal_ids=[goal['id'] for goal in current['goals']]
            goal_dirs=current['_goal_dirs']
            inventory=current['_inventories'][conns.index(commit_conn)]
            roots={key:str(root) for key,root in record_roots(view).items() if key in set(ids)}
            job_id=secrets.token_hex(16);directory=staging_root()/job_id
            directory.mkdir(parents=True)
            threads=[item for inv in current['_inventories'] for item in inv.goal_threads]
            manifest={'version':2,'ids':ids,'roots':roots,'project':project,
                      'journal':'project' if durable else 'global','goal_ids':goal_ids,'goal_dirs':goal_dirs,
                      'goal_threads':threads,
                      'task_threads':[item for inv in current['_inventories'] for item in inv.task_threads]}
            registry.write_json(directory/'manifest.json',manifest)
            try:
                for key in ids:
                    source=safe_directory(Path(roots[key])/key,Path(roots[key]))
                    os.rename(source,directory/key)
                for goal in goal_dirs:
                    (directory/'goal-brain').mkdir(exist_ok=True)
                    source=safe_directory(goal_brain_dir(project,goal),goal_brain_root(project))
                    os.rename(source,directory/'goal-brain'/goal)
                purge_records(commit_conn,current['_scope'],inventory)
                _delete_stage_rows(commit_conn,goal_ids,ids)
                if durable:
                    for key in ids:commit_conn.execute('DELETE FROM tasks WHERE id=?',(key,))
                commit_conn.execute('INSERT INTO task_deletion_log VALUES (?,?,?,?,?,?,?)',
                    (job_id,view['project'],len(ids),len(goal_ids),current['stage_count'],'committed',registry.now()))
            except Exception:
                restore(directory,ids,roots,goal_dirs,project)
                raise
    finally:
        stack.close()
    # The commit precedes irreversible erase. Restart recovery completes a committed
    # purge, or restores uncommitted renamed directories; no user-facing recycle bin.
    PREVIEWS.pop(token,None)
    try:
        _complete(job_id,manifest,directory)
        pending=False
    except (OSError,sqlite3.Error,ValueError):pending=True
    return {'deleted_tasks':len(ids),'deleted_goals':len(goal_ids),'deleted_stages':current['stage_count'],
            'deleted_records':current['records'],'deleted_goal_rounds':len(goal_dirs),
            'cleanup_pending':pending,'note':'删除已提交；暂存物理清理待恢复' if pending else '记录已永久删除，费用总账保留'}
