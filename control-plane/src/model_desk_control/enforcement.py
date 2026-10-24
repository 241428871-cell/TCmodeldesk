"""Read-only adapter declarations, never proof of a particular native process."""

import sys
from enum import StrEnum
from typing import Final, Literal

from model_desk_control.host_flow import HostRecord


class EnforcementScope(StrEnum):
    """Mechanisms are distinct; tool arguments do not imply an OS sandbox."""

    DISPATCH = "dispatch_only"
    TOOLS = "controlled_tools"
    SANDBOX = "native_sandbox"
    UNSUPPORTED = "unsupported"


class EnforcementReport(HostRecord):
    """Describe configured adapter code without promoting readiness to verification."""

    engine: str
    role: str
    enforcement_scope: EnforcementScope
    label: str
    controls: tuple[str, ...]
    limitations: tuple[str, ...]
    basis: Literal["adapter_declaration"] = "adapter_declaration"
    runtime_verified: Literal[False] = False


UNSUPPORTED: Final = EnforcementReport(
    engine="",
    role="",
    enforcement_scope=EnforcementScope.UNSUPPORTED,
    label="未支持",
    controls=(),
    limitations=("此角色没有已声明的适配器约束，不能据此认定工具或文件已隔离。",),
)

CODEX_BRAIN: Final = EnforcementReport(
    engine="",
    role="",
    enforcement_scope=EnforcementScope.SANDBOX,
    label="原生沙箱与隔离副本",
    controls=(
        "配置 Codex read-only 或 workspace-write，auto 使用自动审批审核。",
        "auto 主脑的 cwd 与项目上下文绑定到稳定隔离副本；只有结构化动作进入原项目控制链。",
    ),
    limitations=(
        "审批可扩大单次访问；本声明不证明运行时沙箱已生效。",
        "隔离副本不是任意绝对路径访问的 OS 证明；仍须核对具体运行回执。",
    ),
)

CODEX_WORKER: Final = EnforcementReport(
    engine="",
    role="",
    enforcement_scope=EnforcementScope.SANDBOX,
    label="原生沙箱与候选写回",
    controls=(
        "配置 Codex read-only 或 workspace-write，auto 使用自动审批审核。",
        "编辑任务在临时项目副本中执行，只按声明文件和写回回执应用候选。",
    ),
    limitations=(
        "审批可扩大单次访问；本声明不证明运行时沙箱已生效。",
        "多文件写回可重入但不是全有或全无的文件系统事务。",
    ),
)

CLAUDE_BRAIN: Final = EnforcementReport(
    engine="",
    role="",
    enforcement_scope=EnforcementScope.TOOLS,
    label="部分工具约束",
    controls=(
        "auto 主脑配置 Edit(./.model-desk/**) 拒绝项。",
        "auto 主脑的 cwd 与项目上下文绑定到稳定隔离副本；只有结构化动作进入原项目控制链。",
        "历史只读主脑限制为 Read、Glob、Grep；保留原权限快照。",
    ),
    limitations=(
        "auto 的 Bash、Write 等任意绝对路径仍不由 Edit 规则单独证明受限。",
        "CLI safe mode 与隔离副本不是 OS 沙箱验收回执；仍须核对具体运行。",
    ),
)

CLAUDE_WORKER: Final = EnforcementReport(
    engine="",
    role="",
    enforcement_scope=EnforcementScope.DISPATCH,
    label="派发与回写约束",
    controls=(
        "配置原生 plan 或 auto；执行在排除记录的临时工作副本中。",
        "回写检查声明文件与项目范围，保留独立验收。",
    ),
    limitations=("工作副本不构成 OS 文件隔离；没有工具调用级的路径拒绝保证。",),
)

CLAUDE_MACOS_BRAIN: Final = EnforcementReport(
    engine="",
    role="",
    enforcement_scope=EnforcementScope.SANDBOX,
    label="macOS 写入沙箱与隔离副本",
    controls=(
        "Claude auto 主脑继承 macOS Seatbelt；只允许隔离副本、专用临时区和会话状态写入。",
        "项目外绝对路径写入由操作系统拒绝；只有结构化动作进入原项目控制链。",
    ),
    limitations=(
        "该边界限制写入，不限制原生进程读取；Claude 登录与会话状态目录仍需可写。",
        "适配器声明不代替具体运行回执，非 macOS 宿主仍按工具约束报告。",
    ),
)

CLAUDE_MACOS_WORKER: Final = EnforcementReport(
    engine="",
    role="",
    enforcement_scope=EnforcementScope.SANDBOX,
    label="macOS 写入沙箱与候选写回",
    controls=(
        "Claude 执行器在临时项目副本中继承 macOS Seatbelt，任意子进程共享同一写入边界。",
        "只有声明文件经测试、回执和写回核对后进入原项目。",
    ),
    limitations=(
        "该边界限制写入，不限制读取；Claude 登录与会话状态目录仍需可写。",
        "多文件写回可重入但不是全有或全无的文件系统事务。",
    ),
)

GUARDED_TOOLS: Final = EnforcementReport(
    engine="",
    role="",
    enforcement_scope=EnforcementScope.TOOLS,
    label="工具参数守卫",
    controls=("适配器安装记录保护与工具参数检查，运行回执由原验证链核对。",),
    limitations=(
        "命令与路径参数检查不是 OS 沙箱，不能证明任意子进程均受隔离。",
        "项目或完整访问取决于任务权限快照；此页面没有核验具体运行回执。",
    ),
)

DISPATCH: Final = EnforcementReport(
    engine="",
    role="",
    enforcement_scope=EnforcementScope.DISPATCH,
    label="派发约束",
    controls=("复用工作台任务准入与独立验收入口。",),
    limitations=("没有在此声明工具层或原生沙箱隔离。",),
)

DECLARATIONS: Final = {
    ("codex_cli", "decision"): CODEX_BRAIN,
    ("codex_cli", "execution"): CODEX_WORKER,
    ("claude_cli", "decision"): CLAUDE_BRAIN,
    ("claude_cli", "execution"): CLAUDE_WORKER,
    ("dsh", "execution"): GUARDED_TOOLS,
    ("pi", "execution"): GUARDED_TOOLS,
    ("workbuddy", "execution"): GUARDED_TOOLS,
    ("direct", "execution"): DISPATCH,
    ("aider", "execution"): DISPATCH,
}


def report(engine: str, role: str) -> EnforcementReport:
    """Missing adapters and unsupported decision hosts remain explicitly unsupported."""
    macos = {
        ("claude_cli", "decision"): CLAUDE_MACOS_BRAIN,
        ("claude_cli", "execution"): CLAUDE_MACOS_WORKER,
    }
    declaration = (
        macos.get((engine, role), UNSUPPORTED)
        if sys.platform == "darwin" and (engine, role) in macos
        else DECLARATIONS.get((engine, role), UNSUPPORTED)
    )
    return EnforcementReport(
        engine=engine,
        role=role,
        enforcement_scope=declaration.enforcement_scope,
        label=declaration.label,
        controls=declaration.controls,
        limitations=declaration.limitations,
    )
