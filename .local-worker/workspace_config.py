"""Project/model associations. Reading this module never starts provider work."""
from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Final, Literal, NotRequired, TypedDict, assert_never
from uuid import uuid4

import model_registry as registry
import project_scope
import project_views

CONFIG_NAME: Final = 'workspace.json'
Role = Literal['decision', 'execution']
EFFORTS: Final = ('default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
ROLES: Final = ('decision', 'execution')
MAX_WORK_CONCURRENCY: Final = 64


class ProjectAction(StrEnum):
    ADD = 'add'
    SELECT = 'select'
    RENAME = 'rename'
    REMOVE = 'remove'
    CONCURRENCY = 'concurrency'


class BindingAction(StrEnum):
    SAVE = 'save'
    REMOVE = 'remove'
    ENABLE = 'enable'
    DISABLE = 'disable'


class WorkspaceError(ValueError):
    """A rejected workspace mutation, before any task or model is changed."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class Runtime(TypedDict):
    effort: str
    fast: bool
    autocompact_tokens: int | None
    context_window: int
    thinking_level: NotRequired[str]
    collaboration_hint: NotRequired[str]
    max_instances: NotRequired[int]


class Binding(Runtime):
    id: str
    model_id: str
    role: Role
    enabled: bool
    is_default: bool


class Project(TypedDict):
    path: str
    name: str
    managed: bool
    work_concurrency: int
    bindings: list[Binding]


class Store(TypedDict):
    schema: int
    selected_project: str | None
    projects: list[Project]
    hidden_projects: list[str]


class Mutation(TypedDict, total=False):
    action: str
    path: str
    project: str
    name: str
    model_id: str
    role: Role
    binding_id: str
    id: str
    enabled: bool
    effort: str
    fast: bool
    autocompact_tokens: int | None
    context_window: int
    is_default: bool
    work_concurrency: int
    thinking_level: str
    collaboration_hint: str
    max_instances: int


def defaults() -> Runtime:
    return {'effort': 'medium', 'fast': False, 'autocompact_tokens': 160000,
            'context_window': 200000}


def runtime(values: Mutation, model: dict) -> Runtime:
    """Parse bounded native settings without accepting arbitrary CLI flags."""
    context = values.get('context_window', 200000 if model.get('provider') in ('claude_cli', 'codex_cli')
                         else model.get('max_input_tokens', 32768))
    compact = values.get('autocompact_tokens', int(context * .8) if type(context) is int else None)
    effort = values.get('effort', 'medium')
    fast = values.get('fast', False)
    if (type(context) is not int or not 1024 <= context <= 2_000_000
            or effort not in EFFORTS or type(fast) is not bool
            or (compact is not None and (type(compact) is not int or not 1024 <= compact < context))):
        raise WorkspaceError('请检查 effort、fast、上下文与自动压缩阈值；阈值需小于上下文窗口')
    if fast and model.get('provider') != 'codex_cli':
        raise WorkspaceError('Fast 仅用于 Codex CLI 模型')
    if model.get('provider') == 'claude_cli' and effort in ('none', 'minimal', 'ultra'):
        raise WorkspaceError('Claude CLI 不支持此 effort；请选择 low/medium/high/xhigh/max 或默认')
    if model.get('provider') == 'claude_cli' and compact is not None and not 100000 <= compact <= 1000000:
        raise WorkspaceError('Claude CLI 自动压缩阈值需要在 100000 到 1000000 tokens 之间')
    result: Runtime = {'effort': effort, 'fast': fast, 'autocompact_tokens': compact,
                       'context_window': context}
    if 'thinking_level' in values:
        import thinking_config
        level = values['thinking_level']
        if model.get('provider') == 'dsh' and level != 'default':
            import provider_accounts
            if not provider_accounts.catalog_snapshot('dsh').get('models'):
                provider_accounts.catalog('dsh')
        if level not in thinking_config.profile(model)['supported']:
            raise WorkspaceError('此模型尚未声明支持所选思考等级，请先读取模型能力')
        result['thinking_level'] = level
    if 'collaboration_hint' in values:
        hint = values['collaboration_hint']
        if not isinstance(hint, str) or len(hint) > 12000:
            raise WorkspaceError('分工提示须为不超过 12000 字的文字')
        result['collaboration_hint'] = hint.strip()
    if 'max_instances' in values:
        limit = values['max_instances']
        if type(limit) is not int or not 1 <= limit <= MAX_WORK_CONCURRENCY:
            raise WorkspaceError('模型并行实例上限须为 1 到 64')
        result['max_instances'] = limit
    return result


def _path(value: str, *, existing: bool = True) -> str:
    if existing:
        return project_scope.normalize_project(value)
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise WorkspaceError('项目需要绝对目录路径')
    return str(Path(value).resolve())


def _valid_concurrency(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_WORK_CONCURRENCY


def _read() -> Store:
    path = Path(registry.HERE) / CONFIG_NAME
    if not path.exists():
        return {'schema': 1, 'selected_project': None, 'projects': [], 'hidden_projects': []}
    value = registry.read_json(path)
    if (not isinstance(value, dict) or value.get('schema') != 1
            or not isinstance(value.get('projects'), list)
            or not isinstance(value.get('hidden_projects', []), list)
            or not isinstance(value.get('selected_project'), (str, type(None)))):
        raise WorkspaceError('项目侧栏配置不可读，请修复 workspace.json 后重试')
    for project in value['projects']:
        if (not isinstance(project, dict) or not isinstance(project.get('path'), str)
                or not isinstance(project.get('name'), str) or not isinstance(project.get('bindings'), list)
                or type(project.get('managed', True)) is not bool
                or not _valid_concurrency(project.get('work_concurrency', 1))):
            raise WorkspaceError('项目侧栏配置中的项目结构无效')
        for binding in project['bindings']:
            if (not isinstance(binding, dict) or binding.get('role') not in ROLES
                    or not isinstance(binding.get('model_id'), str) or not isinstance(binding.get('id'), str)
                    or type(binding.get('enabled')) is not bool):
                raise WorkspaceError('项目侧栏配置中的模型关联无效')
            binding.setdefault('is_default', False)
    return {'schema': 1, 'selected_project': value.get('selected_project'),
            'hidden_projects': [p for p in value.get('hidden_projects', []) if isinstance(p, str)],
            'projects': [{'path': p['path'], 'name': p['name'], 'managed': p.get('managed', True),
                          'work_concurrency': p.get('work_concurrency', 1), 'bindings': [
                {key: b[key] for key in Binding.__annotations__ if key in b} for b in p['bindings']]} for p in value['projects']]}


def _with_legacy(store: Store) -> Store:
    """Reuse existing bookmarks without writing or choosing an implicit project."""
    seen = {p['path'] for p in store['projects']} | set(store['hidden_projects'])
    for row in project_views.project_bookmarks():
        path = row['path']
        if path not in seen:
            store['projects'].append({'path': path, 'name': Path(path).name, 'managed': False,
                                      'work_concurrency': 1, 'bindings': []})
            seen.add(path)
    return store


def update_project(body: Mutation) -> Store:
    """Register, rename, select or forget a folder; never modify that folder."""
    action = ProjectAction(body.get('action', 'add'))
    path = _path(body.get('path') or body.get('project') or '', existing=action in ('add', 'select'))
    with registry.file_lock(Path(registry.HERE) / 'workspace.lock'):
        store = _with_legacy(_read())
        project = next((p for p in store['projects'] if p['path'] == path), None)
        match action:
            case ProjectAction.ADD | ProjectAction.SELECT:
                if project is None:
                    import project_defaults
                    concurrency, bindings = project_defaults.new_project()
                    project = {'path': path, 'name': Path(path).name, 'managed': True,
                               'work_concurrency': concurrency, 'bindings': bindings}
                    store['projects'].append(project)
                if action == ProjectAction.ADD:
                    if not project['managed'] and not project['bindings']:
                        import project_defaults
                        project['work_concurrency'], project['bindings'] = project_defaults.new_project()
                    project['managed'] = True
                store['selected_project'] = path
                store['hidden_projects'] = [p for p in store['hidden_projects'] if p != path]
            case ProjectAction.RENAME:
                if project is None:
                    raise WorkspaceError('项目尚未添加到侧栏')
            case ProjectAction.CONCURRENCY:
                if project is None:
                    raise WorkspaceError('项目尚未添加到侧栏')
                value = body.get('work_concurrency')
                if not _valid_concurrency(value):
                    raise WorkspaceError(f'工作并发需要是 1 到 {MAX_WORK_CONCURRENCY} 的整数')
                project['work_concurrency'] = int(value)
            case ProjectAction.REMOVE:
                store['projects'] = [p for p in store['projects'] if p['path'] != path]
                store['hidden_projects'] = list(dict.fromkeys([*store['hidden_projects'], path]))
                if store['selected_project'] == path:
                    store['selected_project'] = None
            case unreachable:
                assert_never(unreachable)
        if 'name' in body and action not in (ProjectAction.REMOVE, ProjectAction.CONCURRENCY):
            name = body['name']
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
                raise WorkspaceError('项目名称需要 1 到 100 个字符')
            project['name'] = name.strip()
        registry.write_json(Path(registry.HERE) / CONFIG_NAME, store)
    return store


def update_binding(body: Mutation) -> Store:
    """Save a role-scoped model association without changing account permission."""
    path = _path(body.get('project', ''))
    role = body.get('role', 'execution')
    if role not in ROLES:
        raise WorkspaceError('请选择决策模型或执行模型')
    with registry.file_lock(Path(registry.HERE) / 'workspace.lock'):
        store = _with_legacy(_read())
        project = next((p for p in store['projects'] if p['path'] == path), None)
        if project is None:
            raise WorkspaceError('请先把项目文件夹添加到侧栏')
        project['managed'] = True
        key = body.get('binding_id') or body.get('id')
        old = next((b for b in project['bindings'] if b['id'] == key), None) if key else next(
            (b for b in project['bindings'] if b['model_id'] == body.get('model_id') and b['role'] == role), None)
        action = BindingAction(body.get('action', 'save'))
        match action:
            case BindingAction.REMOVE:
                if old is None:
                    raise WorkspaceError('项目模型关联不存在')
                project['bindings'].remove(old)
            case BindingAction.ENABLE | BindingAction.DISABLE:
                if old is None:
                    raise WorkspaceError('项目模型关联不存在')
                old['enabled'] = action == 'enable'
            case BindingAction.SAVE:
                model_id = body.get('model_id') or (old['model_id'] if old else '')
                model = next((m for m in registry.list_models()['models'] if m['id'] == model_id), None)
                if model is None:
                    raise WorkspaceError('模型连接不存在，请先添加模型')
                if any(b is not old and b['model_id'] == model_id and b['role'] == role for b in project['bindings']):
                    raise WorkspaceError('此模型已在当前分类中，请编辑已有配置')
                values = {**(old or {}), **body}
                if type(values.get('enabled', True)) is not bool or type(values.get('is_default', False)) is not bool:
                    raise WorkspaceError('启用状态必须是布尔值')
                is_default = values.get('is_default', not any(b['role'] == role for b in project['bindings']))
                binding = {**runtime(values, model), 'id': old['id'] if old else uuid4().hex[:12],
                           'model_id': model['id'], 'role': role, 'enabled': values.get('enabled', True),
                           'is_default': is_default}
                if is_default:
                    for previous in project['bindings']:
                        if previous['role'] == role:
                            previous['is_default'] = False
                if old:
                    project['bindings'].remove(old)
                project['bindings'].append(binding)
            case unreachable:
                assert_never(unreachable)
        registry.write_json(Path(registry.HERE) / CONFIG_NAME, store)
    return store


def dispatch_binding(project: str, model_id: str, binding_id: str = '') -> Binding | None:
    """Apply execution bindings to every controller entry, never another project."""
    if not project:
        if binding_id:
            raise WorkspaceError('项目模型关联需要明确项目目录')
        return None
    path = _path(project)
    store = _with_legacy(_read())
    owner = next((p for p in store['projects'] if p['path'] == path), None)
    rows = owner['bindings'] if owner else []
    binding = next((b for b in rows if b['id'] == binding_id), None) if binding_id else next(
        (b for b in rows if b['model_id'] == model_id and b['role'] == 'execution'), None)
    if binding_id and binding is None:
        raise WorkspaceError('项目模型关联不存在或不属于当前项目')
    if binding and (binding['model_id'] != model_id or binding['role'] != 'execution' or not binding['enabled']):
        raise WorkspaceError('此执行模型未启用、角色不符或与任务模型不一致')
    return dict(binding) if binding else None


def execution_models(project: str, models: list[dict]) -> list[dict]:
    """Constrain adopted project routing; untouched legacy projects keep routing."""
    if not project:
        return models
    path = _path(project)
    owner = next((p for p in _with_legacy(_read())['projects'] if p['path'] == path), None)
    if not owner or (not owner['managed'] and not owner['bindings']):
        return models
    allowed = {b['model_id'] for b in owner['bindings'] if b['role'] == 'execution' and b['enabled']}
    return [m for m in models if m.get('id') in allowed]


def work_concurrency(project: str) -> int:
    """Explicit project worker capacity; new projects use eight, legacy entries retain one."""
    if not project:
        return 1
    path = _path(project)
    owner = next((p for p in _with_legacy(_read())['projects'] if p['path'] == path), None)
    return int(owner.get('work_concurrency', 1)) if owner else 1


def default_model(project: str, role: Role = 'execution') -> str | None:
    """Return only a deliberately selected and enabled project default."""
    if not project:
        return None
    path = _path(project)
    owner = next((p for p in _with_legacy(_read())['projects'] if p['path'] == path), None)
    return next((b['model_id'] for b in owner['bindings'] if b['role'] == role
                 and b['enabled'] and b.get('is_default')), None) if owner else None


def project_config(project: str) -> Project | None:
    """Read one project's saved configuration without aggregating live status.

    Authorization paths may call this while the project's records transaction is
    already open.  Keeping this accessor independent of ``workspace_status``
    prevents a recursive connection to the same records database.
    """
    if not project:
        return None
    path = _path(project)
    return next((p for p in _with_legacy(_read())['projects'] if p['path'] == path), None)


def snapshot(tasks: list[dict] | None = None) -> dict:
    """Return project tree with separate enabled, available and running facts."""
    import project_defaults
    import thinking_config
    import workspace_status
    models = thinking_config.enrich(registry.list_models())['models']
    return {**workspace_status.snapshot(_with_legacy(_read()), models, tasks or [], defaults()),
            'project_defaults': project_defaults.status()}
