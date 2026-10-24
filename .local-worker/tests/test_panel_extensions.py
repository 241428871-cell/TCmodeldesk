"""Actual MCP proxy and asset packager support the project workbench without broad access."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_mcp_server as desk
import panel_bundle


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/workspace"),
        ("GET", "/api/tasks/task_1/resources"),
        ("GET", "/api/governance"),
        ("GET", "/api/engineering-policy"),
        ("GET", "/api/governance/goals/goal_1/recovery"),
        ("GET", "/api/governance/goals/goal_1/progress"),
        ("GET", "/api/flow"),
        ("GET", "/api/flow/sessions/brain%3Around_1?channel=stdout&capture_id="),
        ("GET", "/api/flow/sessions/task%3Atask_1?channel=stderr&capture_id=capture_2"),
        ("GET", "/api/flow/sessions/task%3Atask_1?channel=stdout&capture_id=c&view=events"),
        ("POST", "/api/workspace/projects"),
        ("POST", "/api/workspace/bindings"),
        ("POST", "/api/provider-catalog"),
        ("POST", "/api/accounts/refresh"),
        ("POST", "/api/engineering-policy"),
        ("POST", "/api/governance/goals"),
        ("POST", "/api/governance/goals/goal_1/control"),
        ("POST", "/api/governance/goals/goal_1/contract"),
        ("POST", "/api/governance/goals/goal_1/reconcile"),
        ("POST", "/api/governance/goals/goal_1/interactions"),
        ("POST", "/api/governance/decisions/decision_1/answer"),
    ],
)
def test_declared_ui_routes_reach_existing_proxy(method: str, path: str) -> None:
    assert desk.validate_request(path, method) == path


@pytest.mark.parametrize(
    "path",
    [
        "/api/flow?project=other",
        "/api/tasks/task_1/resources?project=other",
        "/api/tasks/../resources",
        "/api/governance/goals/goal_1/recovery?project=other",
        "/api/flow/sessions/brain%253Around_1",
        "/api/flow/sessions/brain%3A../secrets",
        "/api/flow/sessions/brain%3Around_1?channel=stdin",
        "/api/flow/sessions/brain%3Around_1?channel=stdout&channel=stderr",
        "/api/flow/sessions/task%3Atask_1?capture_id=../other",
        "/api/flow/sessions/task%3Atask_1?capture_id=capture&unknown=1",
        "/api/flow/sessions/task%3Atask_1?channel=stdout&capture_id=one&x=2",
        "/api/flow/sessions/task%3Atask_1?channel=stdout#other",
        "/api/flow/sessions/task%3Atask_1?capture_id=%00",
        "/api/flow/sessions/task%3Atask_1?channel=stdout&view=html",
        "/api/flow/sessions/task%3Atask_1?view=events&view=raw",
        "/api/flow/sessions/unknown%3Atask_1",
        "/api/workspace/secrets",
        "http://[",
    ],
)
def test_proxy_does_not_turn_output_inspection_into_arbitrary_access(path: str) -> None:
    with pytest.raises(desk.PanelError):
        _ = desk.validate_request(path, "GET")


def test_bundle_includes_new_assets_in_declared_execution_order() -> None:
    html = desk.build_workbench_html()
    assert "<script src=" not in html
    assert '<link rel="stylesheet"' not in html
    reconciliation = html.index("window.modelDeskReconciliation =")
    assert reconciliation < html.index("window.modelDeskGovernance=")
    assert html.index("window.modelDeskGovernance=") < html.index("window.modelDeskFlow=")
    assert ".recon-dialog" in html


def test_flow_reuses_goal_progress_and_requirement_controls() -> None:
    panel = Path(panel_bundle.__file__).with_name("panel")
    flow = (panel / "flow.js").read_text()
    governance = (panel / "governance.js").read_text()
    assert 'data-flow-action="progress"' in flow
    assert 'data-flow-action="requirement"' in flow
    assert "openProgress(ref.goal_id)" in flow
    assert "openRequirement(ref.goal_id)" in flow
    assert "['external','工具与子代理']" in flow
    assert "CLI 工具或子代理事件" in flow
    assert "node.status==='exited'?'已完成'" in flow
    assert "window.modelDeskGovernance={refresh,openProgress,openRequirement}" in governance


def test_policy_dialog_distinguishes_saved_config_from_loaded_brain_receipt() -> None:
    governance = (
        Path(panel_bundle.__file__).with_name("panel") / "governance.js"
    ).read_text()
    assert "当前目标实际加载" in governance
    assert "配置已更新 · 待下一轮" in governance
    assert "已加载并确认" in governance
    assert "loaded_snapshot_id.slice(0,12)" in governance


def test_project_model_menu_locates_matching_real_flow_session() -> None:
    panel = Path(panel_bundle.__file__).with_name("panel")
    flow = (panel / "flow.js").read_text()
    workspace = (panel / "workspace.js").read_text()
    assert "查看消息流与控制…" in workspace
    assert "openBindingFlow(context.project,binding)" in workspace
    assert "modelId:binding.model_id,role:binding.role" in workspace
    assert "pendingModel=options.modelId" in flow
    assert "row.model_id===model&&(!role||row.role===role)" in flow
    assert "启用或就绪不代表已经启动" in flow


def test_execution_endpoint_consumes_background_only_runtime_policy() -> None:
    workspace = (
        Path(panel_bundle.__file__).with_name("panel") / "workspace.js"
    ).read_text()
    assert "row.runtime?.ui_policy==='background-only'" in workspace


def test_account_cards_expose_provenance_and_stale_failure_reasons() -> None:
    panel = Path(panel_bundle.__file__).with_name("panel")
    workspace = (panel / "workspace.js").read_text()
    assert "Claude CLI 当前登录账户 · 原生 get_usage" in workspace
    assert "Codex 当前登录账户 · 原生 account/rateLimits/read" in workspace
    assert "DeepSeek 官方 API 账户 · /user/balance" in workspace
    assert "显示上次读取值" in workspace
    assert "非实时数据" in workspace
    assert "accountNotice(native,windows.length>0)" in workspace


def test_task_picker_does_not_claim_native_cli_session_continuation() -> None:
    panel = Path(panel_bundle.__file__).with_name("panel")
    app = (panel / "app.js").read_text()
    html = (panel / "index.html").read_text()
    assert "Claude/Codex/WorkBuddy 执行任务每次新建会话" in app
    assert "当前执行适配器不提供会话续接" in app
    assert "nativeProvider?id!==nativeProvider:nativeAgentIds.includes(id)" in app
    assert "$('task-model').onchange=()=>{taskFields();updateTaskOutputHint();};" in app
    assert 'value="claude_cli">Claude CLI · 原生工具 / 新会话' in html
    assert 'value="codex_cli">Codex CLI · 原生工具 / 新会话' in html


def test_model_dialog_describes_interface_only_check() -> None:
    panel = Path(panel_bundle.__file__).with_name("panel")
    app = (panel / "app.js").read_text()
    html = (panel / "index.html").read_text()
    assert "保存后可用“验证接口”只读检查模型目录；不会发送生成请求。" in html
    assert "连接测试会发送一条很短的生成请求" not in html
    assert "接口验证通过；未调用模型生成" in app


@pytest.mark.parametrize(
    "extra",
    [
        '<script src="/unregistered.js"></script>',
        '<script src="https://example.invalid/remote.js"></script>',
        '<script src="/../other.js"></script>',
        '<script src="/app.js"></script>',
        '<link rel="stylesheet" href="/unregistered.css">',
    ],
)
def test_bundle_rejects_unknown_or_duplicate_resources(tmp_path: Path, extra: str) -> None:
    _ = (tmp_path / "index.html").write_text(
        '<link rel="stylesheet" href="/style.css"><script src="/app.js"></script>' + extra
    )
    for name in ("app.js", "style.css", "codex-bridge.js"):
        _ = (tmp_path / name).write_text("")
    with pytest.raises(panel_bundle.BundleError):
        _ = panel_bundle.bundle(tmp_path, "codex-bridge.js")
