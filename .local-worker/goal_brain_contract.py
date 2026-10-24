"""Structured native-brain actions; invalid output never becomes a fallback plan."""
from __future__ import annotations

import json
from typing import Final, TypedDict

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]
type Record = dict[str, Json]


class BrainError(ValueError):
    """A planner invocation or its proposed actions could not be verified."""


class BrainResult(TypedDict):
    actions: Record
    native_session_id: str
    receipt: str


def _object(properties: Record) -> Record:
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


_TEXT: Final[Record] = {'type': 'string'}
_TEXTS: Final[Record] = {'type': 'array', 'items': _TEXT}
_EVIDENCE: Final = _object({'source': {'type': 'string', 'enum': ['reviewer_inspection', 'reviewer_file', 'reviewer_test']},
                          'detail': _TEXT, 'exit_code': {'type': ['integer', 'null']}})
_CRITERION: Final = _object({'id': _TEXT, 'status': {'type': 'string', 'enum': ['pass', 'fail']},
                           'evidence': {'type': 'array', 'items': _EVIDENCE}, 'note': _TEXT})
_STAGE: Final = _object({'key': _TEXT, 'title': _TEXT, 'objective': _TEXT,
                       'kind': {'type': 'string', 'enum': ['chat', 'edit']},
                       'depends_on': _TEXTS, 'files': _TEXTS, 'acceptance': _TEXTS,
                       'candidates': _TEXTS, 'fixed_model': _TEXT})
_LEGACY_PLAN: Final = _object({'base_revision': {'type': 'integer'}, 'reason': _TEXT,
                      'stages': {'type': 'array', 'items': _STAGE}})
_BRIEF: Final = _object({'title': _TEXT, 'objective': _TEXT, 'criteria': _TEXTS, 'obligations': _TEXTS})
_PLAN: Final = _object({'base_revision': {'type': 'integer'}, 'reason': _TEXT,
                      'stages': {'type': 'array', 'items': _STAGE},
                      'brief': {'anyOf': [_BRIEF, {'type': 'null'}]}})
_QUESTION: Final = _object({'question': _TEXT, 'reason': _TEXT, 'options': _TEXTS,
                          'recommendation': _TEXT, 'approval_option': _TEXT, 'evidence_refs': _TEXTS,
                          'affected_stage_ids': _TEXTS})
_REVIEW: Final = _object({'task_id': _TEXT,
                        'decision': {'type': 'string', 'enum': ['accept', 'return']},
                        'criteria': {'type': 'array', 'items': _CRITERION},
                        'problems': _TEXT, 'boundaries': _TEXT, 'notes': _TEXT})
_OBLIGATION: Final = _object({'id': _TEXT, 'status': {'type': 'string', 'enum': ['fulfilled']},
                            'evidence': _TEXT})
ACTION_SCHEMA: Final = _object({'plan': {'anyOf': [_PLAN, _LEGACY_PLAN, {'type': 'null'}]},
    'questions': {'type': 'array', 'items': _QUESTION}, 'dispatch_stage_ids': _TEXTS,
    'reviews': {'type': 'array', 'items': _REVIEW},
    'obligations': {'type': 'array', 'items': _OBLIGATION},
    'complete': {'type': 'boolean'}, 'summary': _TEXT})


def _validate(value: Json, schema: Record, path: str) -> None:
    if 'anyOf' in schema:
        for choice in schema['anyOf']:
            try:
                _validate(value, choice, path)
                return
            except BrainError:
                continue
        raise BrainError('主脑动作类型无效：' + path)
    names = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
    matches = {'null': value is None, 'string': isinstance(value, str), 'boolean': type(value) is bool,
               'integer': type(value) is int, 'array': isinstance(value, list), 'object': isinstance(value, dict)}
    if not any(matches.get(name, False) for name in names):
        raise BrainError('主脑动作类型无效：' + path)
    if 'enum' in schema and value not in schema['enum']:
        raise BrainError('主脑动作值未授权：' + path)
    if isinstance(value, dict):
        properties = schema['properties']
        if set(value) != set(schema['required']):
            raise BrainError('主脑动作字段不完整或包含未授权字段：' + path)
        for name, child in value.items():
            _validate(child, properties[name], path + '.' + name)
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate(child, schema['items'], path + '[' + str(index) + ']')


def parse_actions(raw: str) -> Record:
    """Reject malformed transport, unknown action fields and fabricated phase defaults."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise BrainError('主脑没有返回有效 JSON；保留当前计划，等待核对') from error
    _validate(value, ACTION_SCHEMA, 'actions')
    if not isinstance(value, dict) or set(value) != set(ACTION_SCHEMA['required']):
        raise BrainError('主脑动作字段不完整或包含未授权字段')
    if not isinstance(value['summary'], str) or type(value['complete']) is not bool:
        raise BrainError('主脑结论格式无效')
    for name in ('questions', 'dispatch_stage_ids', 'reviews', 'obligations'):
        if not isinstance(value[name], list):
            raise BrainError('主脑动作必须是列表：' + name)
    if any(not isinstance(item, str) for item in value['dispatch_stage_ids']):
        raise BrainError('派发阶段标识无效')
    if value['plan'] is not None:
        plan = value['plan']
        if not isinstance(plan, dict) or set(plan) not in (
                {'base_revision', 'reason', 'stages'}, {'base_revision', 'reason', 'stages', 'brief'}):
            raise BrainError('计划修订不得改变人类准则或权限')
        if type(plan['base_revision']) is not int or not isinstance(plan['reason'], str) or not isinstance(plan['stages'], list):
            raise BrainError('计划修订格式无效')
    if any(not isinstance(item, dict) for name in ('questions', 'reviews') for item in value[name]):
        raise BrainError('审核与提问动作格式无效')
    return value


def prompt(context: Record, permission_mode: str = 'read_only') -> str:
    """The brain chooses the decomposition from evidence, without a phase template."""
    instructions = '''你是此项目的主脑。根据目标、当前成果、人类固定准则和有效工程习惯，自主拆分、修订、调度工作。
仅返回 JSON_SCHEMA 对应的 JSON。不要输出预制阶段；每一阶段必须对当前目标有具体作用。
准则、允许文件范围、付费授权、用户暂停/停止、模型绑定是人类权限，你不能降低或绕过。
context.contract.file_scope="project" 时，你可在本项目中自主选择每个阶段的具体文件，不需要人类预先枚举；"selected" 时必须保留 allowed_files 的明确限制，不能把旧目标解释为项目授权。每个编辑阶段仍须列出真实的项目相对文件，工作台记录、账户配置与运行 home 不能列为交付文件。
指令优先级：当前人类目标、固定准则与明确授权 > context.project_guidance 中实际加载的项目规则 > 已选择的全局工程习惯。project_guidance 含根目录规则原文与 SHA-256；在拆分涉及子目录的阶段前，用只读工具继续检查相关嵌套 AGENTS.md/CLAUDE.md，不要假定不存在或读取其他项目和账户文件。
context.latest_requirements 是人类在目标运行中追加的、按版本排序的需求。pending 项必须合并进现有目标，保留不冲突的旧要求与已完成成果；direction_change 允许调整方向但不等于取消目标。根据它们修订完整活动计划、依赖和验收条件，不要新建重复目标或重复调用已完成工作。
plan 为完整的活动阶段快照；保持仍需的阶段 key 和运行中/已完成阶段。没有修订时 plan=null。
用户可只填写目标名称、期望结果、验收准则中的一部分。context.contract.brief_input 与 criteria 是人类原始输入；completion_pending 列出待补充项。首次规划先调查项目事实，在 plan.brief 提交 title、objective、criteria、obligations：已有人类标题/结果时对应值留空，不得改写；criteria 仅填你补充的可核验准则，obligations 填必要收尾事项（可为空）。补充内容会标为 AI 并持久保存，不会变成人类固定要求。缺少普通实施细节时自主判断，确实无法确定用户意图或需要额外授权时才提交 questions。
AI 补充不得扩大文件范围、模型候选、费用或审核权限，也不得缩短续跑周期。AI 收尾事项会进入既有义务清单并保持 pending；为其安排收尾阶段，读取真实交付物核验后再按清单 id 提交完成证据。后续计划仍须保留未完成的人类及 AI 收尾义务，不能通过省略 brief 或重写计划使其消失。
context.collaboration 是人类可编辑的模型分工提示与并行策略。结合每个执行模型、项目角色提示、任务边界与真实结果选择模型；这些是偏好，不能扩大模型候选、权限或费用授权。同一模型可以启动多个独立实例；无依赖且文件不冲突的阶段可以一起派发，遵守项目、账户与模型实例上限。
若分工提示要求另一模型参与审查，可建立独立只读审查阶段，使用已绑定且已授权的候选。最终验收仍由当前主脑核对实际成果与证据，不能把审查模型的报告直接当作通过。
执行器完成不是独立验收。先读取实际交付文件和结果，按每条验收条件给独立 evidence；不得把执行器测试自报冒充独立测试。
只对 context.tasks 的本目标任务提交 reviews；执行器自报文本本身不能作为通过证据。退回要给问题和整改边界。
验收通过时 notes 应概括实际交付结果、关键接口或决定和遗留限制；系统将独立核验证据与真实文件 SHA-256 保存为后续阶段的交接，notes 摘要本身只算说明。
dispatch_stage_ids 可用阶段 id 或新计划的 key。只能调度依赖通过验收、没有运行中任务的阶段。
阶段 candidates/fixed_model 只能从 context.execution_models 中选择。CLI 账户候选必须明确写入 candidates，不能依赖未知费用的隐式自动选择；没有已授权可用的执行模型时提交例外，不要虚构模型标识。
退回阶段仍用同一个阶段 id 提交 dispatch_stage_ids，系统通过原任务的 rework_of 派发并保留原模型、会话策略和唯一整改次数；不可新建等价阶段绕过整改上限。
questions 仅用于不能在固定准则内解决的权限/范围/取舍异常，需选项、推荐、证据和受影响阶段；普通计划修订不需要人类批准。
每个问题必须填写 approval_option，值为 options 中明确允许当前方案继续的那个选项原文；其他选项返回主脑修订。推荐选项与批准选项可以不同，选项位置不代表批准。每个选项都应清楚写明后果，不能把“拒绝”“先补充信息”或无关选择暗中当作批准当前方案。
context.clock 包含此目标唯一正式时钟、授权状态和收尾义务。obligations 动作只能提交既有义务的 id、status="fulfilled" 和具体 evidence；新补充义务须先经 plan.brief 保存，下一轮取得真实 id 后再核验完成，不能取消既有义务。
逐项读取实际交付文件、报告、归档或清理结果，独立核验后才提交义务完成证据；evidence 应写明核对的路径、实物内容和结论，不能以执行器自报或“已经完成”代替。无法核验时保留 pending，必要时拆分收尾阶段或提交例外问题。
complete 仅在所有活动阶段独立验收通过、交付满足固定准则且所有未被人类取消的收尾义务都有核验证据时为 true。尚有 pending 义务时系统保留目标和原续跑周期，不会结项；若需要最终人工审核，系统会提交审核收件箱。
元数据发现不是任务。
'''
    return (instructions + TOOL_POLICY.get(permission_mode, TOOL_POLICY['read_only']) + '\nJSON_SCHEMA:\n'
            + json.dumps(ACTION_SCHEMA, ensure_ascii=False) + '\nCONTEXT:\n' + json.dumps(context, ensure_ascii=False))


# One policy per native permission snapshot; both hosts receive the same text.
TOOL_POLICY = {
    'auto': ('你以原生 auto 权限运行，原生审批拒绝的操作不要换方式绕过。可用原生文件与终端工具读取、搜索，'
             '并运行不改变交付物的检查命令（测试、构建、静态检查）做调查和独立验收；亲自运行的测试可提交 '
             'reviewer_test 证据，写明命令并给出真实 exit_code。不要直接编辑交付文件：改动通过阶段派发由执行器完成，'
             '再由你独立验收。不得写入 .model-desk 工作台记录、账户配置或运行 home，不发消息、不发起其他模型调用、'
             '不创建续跑时钟。'),
    'read_only': ('你以只读权限运行，只能读取项目，只能提交 reviewer_file 或 reviewer_inspection 证据。'
                  '不要编辑文件、发消息、发起模型调用、执行工具副作用或创建续跑时钟。'),
}
