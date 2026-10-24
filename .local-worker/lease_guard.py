"""Keep held or unreadable execution resources out of acceptance and history deletion."""

import control_bootstrap
import model_registry as registry
from pydantic import JsonValue

type Record = dict[str, JsonValue]


def reason(task: Record) -> str:
    """Legacy tasks have no lease; protocol tasks require a matching released receipt."""
    if "execution_lease_token" not in task and not task.get("execution_lease_protocol"):
        return ""
    token = task.get("execution_lease_token")
    if not isinstance(token, str) or not token:
        return "持久资源租约身份缺失，需核对原执行"
    control_bootstrap.bind_source()
    import lease_runtime

    from model_desk_control.durable_lease_models import LeasePhase

    try:
        receipt = lease_runtime.current(token)
    except (OSError, ValueError):
        return "持久资源租约不可读，不能假定子进程已经退出"
    if receipt.request.owner != task.get("id") or receipt.request.project != (
        task.get("project") or "(unbound)"
    ):
        return "持久资源租约与原任务身份不一致"
    return "" if receipt.phase == LeasePhase.RELEASED else "执行资源尚未释放，需先确认子进程静止"


def goal_reason(project: str, goal_id: str) -> str:
    """Cancelled task labels cannot hide a held worker during owner handoff."""
    control_bootstrap.bind_source()
    import lease_storage

    from model_desk_control import durable_leases

    try:
        with lease_storage.readonly() as database:
            held = durable_leases.active(database) if database is not None else ()
        for receipt in held:
            if receipt.request.project != project:
                continue
            task = registry.read_task(registry.task_dir(receipt.request.owner, project))
            if task.get("goal_id") == goal_id:
                return "目标仍有持久资源租约，不能移交主脑"
    except (OSError, ValueError):
        return "无法核对目标的持久资源租约，不能移交主脑"
    return ""
