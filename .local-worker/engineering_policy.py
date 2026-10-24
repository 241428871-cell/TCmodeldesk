"""Versioned policy selection; reading policy never applies rules or starts work."""
import hashlib
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Final

import model_registry as registry
import project_storage
from engineering_rules import RULES

CONFIG_NAME: Final = 'engineering-policy.json'
_IDS: Final = tuple(rule['id'] for rule in RULES)
_GLOBAL_FIELDS: Final = frozenset({'global_enabled', 'global_rule_ids', 'global_custom_rules'})
_PROJECT_FIELDS: Final = frozenset({'mode', 'rule_ids', 'custom_rules'})


class PolicyError(ValueError):
    """Invalid selection or a stale policy edit."""


def _ids(value: list) -> list[str]:
    if not isinstance(value, list) or any(item not in _IDS for item in value):
        raise PolicyError('包含未知工程习惯类别')
    return list(dict.fromkeys(value))


def _text(value: str) -> str:
    if not isinstance(value, str) or len(value) > 24000:
        raise PolicyError('自定义工程习惯须为不超过 24000 字的文字')
    return value.strip()


def _config() -> dict:  # noqa: DICT_OK - JSON boundary shared with panel/MCP.
    path = Path(registry.HERE) / CONFIG_NAME
    if not path.is_file():
        return {'schema': 1, 'revision': 0, 'global_enabled': False,
                'global_rule_ids': list(_IDS), 'global_custom_rules': '', 'projects': {}}
    data = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(data, dict) or data.get('schema') != 1
            or isinstance(data.get('revision'), bool) or not isinstance(data.get('revision'), int)
            or data['revision'] < 0 or not isinstance(data.get('global_enabled'), bool)
            or not isinstance(data.get('projects', {}), dict)):
        raise PolicyError('工程习惯配置格式无效，请保留原文件后修复')
    return {'schema': 1, 'revision': data['revision'],
            'global_enabled': data['global_enabled'],
            'global_rule_ids': _ids(data.get('global_rule_ids', list(_IDS))),
            'global_custom_rules': _text(data.get('global_custom_rules', '')),
            'projects': data.get('projects', {})}


def _project_path(project: str) -> Path:
    desk = project_storage.desk_dir(project)
    path = desk / CONFIG_NAME
    if desk.is_symlink() or path.is_symlink():
        raise PolicyError('项目工程习惯记录路径不可信，已拒绝读取或保存')
    return path


def _project_config(project: str, global_config: dict) -> dict:  # noqa: DICT_OK
    path = _project_path(project)
    source = 'project'
    if path.is_file():
        data = registry.read_json(path)
    else:
        data = global_config.get('projects', {}).get(project)
        source = 'legacy_global' if data is not None else 'default'
    if data is None:
        return {'schema': 1, 'revision': 0, 'mode': 'inherit',
                'rule_ids': list(_IDS), 'custom_rules': '', 'storage': source}
    if not isinstance(data, dict):
        raise PolicyError('项目工程习惯配置格式无效，请保留原文件后修复')
    revision = data.get('revision', 0)
    mode = data.get('mode', 'inherit')
    if (isinstance(revision, bool) or not isinstance(revision, int) or revision < 0
            or mode not in ('inherit', 'off', 'custom')):
        raise PolicyError('项目工程习惯配置格式无效，请保留原文件后修复')
    return {'schema': 1, 'revision': revision, 'mode': mode,
            'rule_ids': _ids(data.get('rule_ids', list(_IDS))),
            'custom_rules': _text(data.get('custom_rules', '')), 'storage': source}


def _combined_revision(global_revision: int, project_revision: int) -> int:
    total = global_revision + project_revision
    return total * (total + 1) // 2 + project_revision


def save_policy(body: dict) -> dict:  # noqa: DICT_OK - explicit panel/MCP mutation.
    """Save an explicit global/project selection without editing host settings."""
    if not isinstance(body, dict):
        raise PolicyError('工程习惯配置须为对象')
    project = project_storage.normalize(body['project']) if body.get('project') else ''
    scope = body.get('scope')
    has_global = bool(_GLOBAL_FIELDS & body.keys())
    has_project = bool(_PROJECT_FIELDS & body.keys())
    if scope is None:
        scope = 'project' if has_project else 'global'
    if scope not in ('global', 'project') or (scope == 'global' and has_project) or (scope == 'project' and has_global):
        raise PolicyError('全局模板和项目覆盖须按各自范围保存')
    if scope == 'project':
        if not project:
            raise PolicyError('保存项目工程习惯需要明确项目目录')
        path = _project_path(project)
        with registry.file_lock(path.with_name('engineering-policy.lock')):
            global_config = _config()
            prior = _project_config(project, global_config)
            if 'base_revision' in body and body['base_revision'] != prior['revision']:
                raise PolicyError('项目工程习惯已更新，请刷新后重新编辑')
            mode = body.get('mode', prior['mode'])
            if mode not in ('inherit', 'off', 'custom'):
                raise PolicyError('项目工程习惯模式应为 inherit、off 或 custom')
            updated = {'schema': 1, 'revision': prior['revision'], 'mode': mode,
                       'rule_ids': _ids(body.get('rule_ids', prior['rule_ids'])),
                       'custom_rules': _text(body.get('custom_rules', prior['custom_rules']))}
            comparable = {key: prior[key] for key in ('mode', 'rule_ids', 'custom_rules')}
            if {key: updated[key] for key in comparable} != comparable or prior['storage'] != 'project':
                updated['revision'] += 1
                registry.write_json(path, updated)
        with registry.file_lock(Path(registry.HERE) / 'engineering-policy.lock'):
            global_config = _config()
            if project in global_config['projects']:
                del global_config['projects'][project]
                registry.write_json(Path(registry.HERE) / CONFIG_NAME, global_config)
        return policy_snapshot(project)

    with registry.file_lock(Path(registry.HERE) / 'engineering-policy.lock'):
        data = _config()
        if 'base_revision' in body and body['base_revision'] != data['revision']:
            raise PolicyError('全局工程习惯已更新，请刷新后重新编辑')
        before = {key: data[key] for key in _GLOBAL_FIELDS}
        if 'global_enabled' in body:
            if not isinstance(body['global_enabled'], bool):
                raise PolicyError('全局嵌入开关须为布尔值')
            data['global_enabled'] = body['global_enabled']
        if 'global_rule_ids' in body:
            data['global_rule_ids'] = _ids(body['global_rule_ids'])
        if 'global_custom_rules' in body:
            data['global_custom_rules'] = _text(body['global_custom_rules'])
        if {key: data[key] for key in _GLOBAL_FIELDS} != before:
            data['revision'] += 1
            registry.write_json(Path(registry.HERE) / CONFIG_NAME, data)
    return policy_snapshot('')


def policy_snapshot(project: str = '') -> dict:  # noqa: DICT_OK - immutable JSON snapshot.
    """Describe current selection; no files, host configuration or tasks are changed."""
    project = project_storage.normalize(project) if project else ''
    data = _config()
    selected = _project_config(project, data) if project else {
        'revision': 0, 'mode': 'inherit', 'rule_ids': list(_IDS),
        'custom_rules': '', 'storage': 'unbound'}
    mode = selected['mode']
    enabled = mode == 'custom' or (mode == 'inherit' and data['global_enabled'])
    ids = selected['rule_ids'] if mode == 'custom' else data['global_rule_ids']
    custom = selected['custom_rules'] if mode == 'custom' else data['global_custom_rules']
    effective = deepcopy([rule for rule in RULES if enabled and rule['id'] in ids])
    if enabled and custom:
        effective.append({'id': 'custom', 'title': '用户自定义工程习惯', 'text': custom,
                          'source': '项目覆盖' if mode == 'custom' else '全局自定义'})
    revision = _combined_revision(data['revision'], selected['revision'])
    result = {'schema': 1, 'revision': revision,
              'global_revision': data['revision'], 'project_revision': selected['revision'],
              'project_storage': selected['storage'], 'project': project,
              'global_enabled': data['global_enabled'], 'global_rule_ids': data['global_rule_ids'],
              'global_custom_rules': data['global_custom_rules'], 'configured_mode': mode,
              'effective_mode': mode if enabled else 'off', 'rule_ids': selected['rule_ids'],
              'custom_rules': selected['custom_rules'], 'effective_rules': effective,
              'available_rules': deepcopy(list(RULES)), 'sources': list(dict.fromkeys(rule['source'] for rule in effective)),
              'precedence': ['当前用户要求与固定准则', '项目约定', '已选择的全局工程习惯'],
              'load_state': 'available_for_new_tasks',
              'note': '此处是当前可选规则；实际任务保存各自加载快照。修改不改写既有任务，也不授予执行或费用权限。'}
    encoded = json.dumps({key: result[key] for key in ('revision', 'project', 'effective_mode', 'effective_rules')},
                         sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    import collaboration_policy
    delivery = collaboration_policy.snapshot()['skill_mode']
    result['delivery'] = delivery
    result['snapshot_id'] = hashlib.sha256(encoded + (':' + delivery).encode()).hexdigest()
    return result


def task_context(project: str = '') -> dict:  # noqa: DICT_OK - caller persists this snapshot on its new task.
    """Return an isolated prompt/snapshot for a new task; snapshots grant no authority."""
    snapshot = policy_snapshot(project)
    import collaboration_policy
    delivery = collaboration_policy.snapshot()['skill_mode']
    if delivery != 'inline':
        # UI catalogs contain the complete templates; native task contexts do
        # not need those bodies merely to ask the CLI to load its installed skill.
        snapshot.pop('available_rules', None)
        snapshot['effective_rules'] = [{key: row[key] for key in ('id', 'title', 'source')}
                                       for row in snapshot['effective_rules']]
        snapshot['delivery'] = delivery
        cue = ('请使用已安装的 tcauto（TCAUTO）skill 完成本次分配的任务。先读取实际 SKILL.md；'
               '如果未找到或不支持技能入口，明确记录未加载，不得声称已应用。'
               '跨模型调度、独立验收与续接由工作台负责；不要另建定时器、重复派发或自行扩大授权。'
               '当前用户需求与项目准则优先。') if delivery == 'native' else ''
        if delivery == 'native' and snapshot.get('effective_mode') != 'off':
            custom = snapshot['custom_rules'] if snapshot['configured_mode'] == 'custom' else snapshot['global_custom_rules']
            if custom:
                cue += '\n用户明确保存的自定义工程要求：\n' + custom
        return {'prompt': cue, 'snapshot': snapshot, 'snapshot_id': snapshot['snapshot_id']}
    rules = snapshot['effective_rules']
    prompt = ''
    if rules:
        prompt = ('已选择的跨项目工程习惯（优先级：当前用户要求与固定准则 > 项目约定 > 下列指导）。'
                  '只在既有授权内应用；当前暂停、STOP、费用授权和人类审核不可由快照绕过。\n'
                  + '\n'.join('• ' + rule['title'] + '：' + rule['text'] for rule in rules))
    return {'prompt': prompt, 'snapshot': snapshot, 'snapshot_id': snapshot['snapshot_id']}


def goal_status(project: str, goal_id: str, current_snapshot_id: str) -> dict:  # noqa: DICT_OK
    """Report the latest policy actually captured by this Goal's native round.

    A saved selection is not proof that a running brain loaded it.  The durable
    graph snapshot is the authority; a completed native step confirms that the
    model returned after receiving that snapshot.
    """
    base = {'status': 'not_loaded', 'current_snapshot_id': current_snapshot_id,
            'loaded_snapshot_id': '', 'loaded_revision': None, 'confirmed': False,
            'is_current_round': False, 'message': '主脑尚未加载工程习惯快照'}
    import continuation_store
    try:
        with continuation_store.readonly(project) as database:
            if database is None:
                return base
            tables = {row[0] for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('continuation_clocks','continuation_intents','goal_graph_steps')"
            )}
            if tables != {'continuation_clocks', 'continuation_intents', 'goal_graph_steps'}:
                return base
            row = database.execute(
                """SELECT i.id AS intent_id,
                          COALESCE(json_extract(c.data,'$.active_intent_id'),'') AS active_intent_id,
                          json_extract(s.result_json,'$.context.engineering_policy.snapshot_id')
                              AS loaded_snapshot_id,
                          json_extract(s.result_json,'$.context.engineering_policy.snapshot.revision')
                              AS loaded_revision,
                          COALESCE(n.status,'') AS native_status
                   FROM continuation_intents i
                   JOIN goal_graph_steps s ON s.intent_id=i.id
                        AND s.step='snapshot' AND s.status='done'
                   LEFT JOIN continuation_clocks c ON c.goal_id=i.goal_id
                   LEFT JOIN goal_graph_steps n ON n.intent_id=i.id AND n.step='native'
                   WHERE i.goal_id=? ORDER BY i.rowid DESC LIMIT 1""",
                (goal_id,),
            ).fetchone()
        if row is None:
            return base
        loaded_id = row['loaded_snapshot_id']
        revision = row['loaded_revision']
        if not isinstance(loaded_id, str):
            raise ValueError('invalid policy snapshot')
        if not loaded_id or isinstance(revision, bool) or not isinstance(revision, int):
            raise ValueError('invalid policy snapshot')
        active_intent = str(row['active_intent_id'] or '')
        is_current = not active_intent or active_intent == row['intent_id']
        confirmed = row['native_status'] == 'done' and is_current
        matches = loaded_id == current_snapshot_id
        status = ('stale' if not matches else 'confirmed' if confirmed
                  else 'captured' if is_current else 'starting')
        messages = {
            'stale': '配置已更新；现有轮次保留旧快照，下一轮加载新版本',
            'confirmed': '主脑已使用当前快照完成一轮并返回原生回执',
            'captured': '当前轮次已保存输入快照，正在等待原生主脑回执',
            'starting': '新轮次尚未保存快照；此处显示上一轮实际加载版本',
        }
        return {**base, 'status': status, 'loaded_snapshot_id': loaded_id,
                'loaded_revision': revision, 'confirmed': confirmed,
                'is_current_round': is_current, 'message': messages[status]}
    except (KeyError, OSError, TypeError, ValueError, sqlite3.Error):
        return {**base, 'status': 'unreadable',
                'message': '主脑工程习惯回执无法核对；不会假定已经加载'}
