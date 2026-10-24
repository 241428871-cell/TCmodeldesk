"""On-demand, read-only resource observations for the existing scoped task detail."""

from pathlib import Path

import cli_writeback
import lease_descendants
import lease_processes
import lease_runtime
import lease_storage
import model_registry as registry
from model_desk_control import file_writeback, writeback_receipt
from model_desk_control.durable_lease_models import LeasePhase
from model_desk_control.native_output import owned_path, read_metadata
from pydantic import JsonValue

type Record = dict[str, JsonValue]


def _lease(state: Record) -> Record:
    token = state.get("execution_lease_token")
    if token is None and not state.get("execution_lease_protocol"):
        return {"state": "legacy", "label": "历史任务，无持久租约证明"}
    if not isinstance(token, str) or not token:
        return {"state": "unknown", "label": "租约身份缺失"}
    receipt = lease_runtime.current(token)
    if (receipt.request.owner, receipt.request.project) != (
        state.get("id"), state.get("project") or "(unbound)"
    ):
        return {"state": "unknown", "label": "租约与当前任务不匹配"}
    labels = {
        LeasePhase.RESERVED: "资源已预留，尚未启动",
        LeasePhase.ARMED: "已进入启动边界，等待工作器登记",
        LeasePhase.STARTED: "工作器已登记，资源仍保留",
        LeasePhase.RELEASED: "此代资源已释放",
    }
    result: Record = {"state": receipt.phase.value, "label": labels[receipt.phase]}
    if receipt.phase == LeasePhase.STARTED:
        result["worker_exited"] = (
            lease_processes.exited(receipt.launcher) if receipt.launcher else False
        )
        result["session_quiet"] = lease_processes.quiet(receipt)
        with lease_storage.readonly() as database:
            observed = lease_descendants.read(database, receipt) if database is not None else None
        observation = "后代观察记录缺失"
        if observed is not None:
            states = {
                "observing": "未完成末次核对", "closed": "已完成末次核对", "unknown": "观察中断",
            }
            observation = (
                f"已记录 {len(observed.descendants)} 个确定后代、"
                f"{len(observed.suspected_orphans)} 个疑似逃逸进程，{states[observed.state]}"
            )
            result["descendant_count"] = len(observed.descendants)
            result["suspected_orphan_count"] = len(observed.suspected_orphans)
            result["fork_guard"] = observed.fork_guard
            result["observation_state"] = observed.state
            if observed.uncertainty_reason:
                result["uncertainty_reason"] = observed.uncertainty_reason
        result["scope"] = (
            observation
            + (
                "；核对受管会话、已观察后代及 macOS 内核 fork 通知后的近期重新托管候选。"
                "疑似候选只阻止释放，不自动停止。"
                if observed is not None and observed.fork_guard == "darwin_kqueue"
                else "；核对受管会话及已观察后代；当前平台只有采样证据。"
            )
            if receipt.containment == "posix_session" else "没有可用的受管会话证明。"
        )
    return result


def _writeback(run: Path, state: Record) -> Record:
    if state.get("writeback_protocol") is None and not state.get("writeback_capture_id"):
        return {"state": "absent", "label": "没有保存文件写回协议"}
    task = cli_writeback.RecoveryTask.model_validate(state)
    root = Path(task.project)
    if str(root.resolve()) != task.project or run != owned_path(
        root, Path(".model-desk/runs") / task.id
    ):
        return {"state": "unknown", "label": "文件回执与项目路径不匹配"}
    directory = owned_path(run, Path("native-cli") / task.writeback_capture_id)
    digest = writeback_receipt.input_digest(task.id, state, read_metadata(run / "prompt.txt"))
    receipt, plan = writeback_receipt.load(
        directory, digest, task.id, task.writeback_capture_id, tuple(task.files)
    )
    file_writeback.require_baseline(directory / "proposed-files", plan.proposed)
    observed = file_writeback.snapshot(root, tuple(item.name for item in plan.baseline))
    cancelled = state.get("status") == "cancelled" or (run / "cancelled.flag").exists()
    rows: list[JsonValue] = []
    for old, new, current in zip(plan.baseline, plan.proposed, observed, strict=True):
        status = (
            "unchanged" if old == new else "applied" if current == new
            else "pending" if current == old else "conflict"
        )
        if cancelled and status == "pending":
            status = "retained"
        rows.append({"name": old.name, "state": status})
    return {
        "state": "retained" if cancelled else "verified",
        "label": "任务已取消，候选只保留、不继续写回" if cancelled else "已核对保存回执与文件版本",
        "files": rows, "test_recorded": receipt.test is not None,
        "note": "取消意图保持；原候选保留供查看，不自动恢复。" if cancelled else
                "仅恢复保存的文件，不调用模型；执行时仍重新核对授权、租约和文件。",
    }


def describe(run: Path) -> Record:
    """Never reconcile, acquire, dispatch, query accounts or create a missing store."""
    state = registry.read_task(run)
    result: Record = {"task_id": state.get("id"), "observed_at": registry.now()}
    try:
        result["lease"] = _lease(state)
    except (OSError, ValueError):
        result["lease"] = {"state": "unknown", "label": "租约暂时不可核对，资源不视为空闲"}
    try:
        result["writeback"] = _writeback(run, state)
    except (OSError, ValueError):
        result["writeback"] = {
            "state": "unknown", "label": "保存回执、测试或候选文件未通过核对",
            "note": "保留原成果；不要重新运行模型来代替恢复。",
        }
    return result
