"""Stop only durably attributed descendants through the original cancellation path."""

import os

import lease_descendants
import lease_runtime
import lease_storage
import psutil
from model_desk_control.durable_lease_models import DurableLease, LeasePhase
from model_desk_control.host_queue_models import Process
from pydantic import JsonValue

type Record = dict[str, JsonValue]


def _signal(identity: Process) -> bool:
    """PID reuse means the original process is gone, never permission to kill its replacement."""
    if identity.pid == os.getpid():
        return False
    try:
        process = psutil.Process(identity.pid)
        if process.create_time() != identity.birth or process.status() == psutil.STATUS_ZOMBIE:
            return True
        process.kill()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return True
    except (psutil.Error, OSError):
        return False
    return True


def stop_recorded(task: Record) -> Record:
    """A stop request is not exit proof; reconcile separately under the lease transaction."""
    token = task.get("execution_lease_token")
    if not token and not task.get("execution_lease_protocol"):
        return {"state": "legacy", "note": "历史执行器沿用原停止路径"}
    if not isinstance(token, str) or not token:
        return {"state": "unknown", "note": "租约身份缺失，不能核对残留进程"}
    try:
        receipt = lease_runtime.current(token)
        if (receipt.request.project, receipt.request.owner) != (
            task.get("project") or "(unbound)", task.get("id")
        ):
            return {"state": "unknown", "note": "租约不属于当前任务，未操作其进程"}
        return _stop_owned(receipt)
    except (OSError, ValueError):
        return {"state": "unknown", "note": "后代记录暂时不可核对，停止意图保留，租约不自动释放"}


def _stop_owned(receipt: DurableLease) -> Record:
    """Signal the already-scoped observation; keep uncertainty separate from cancellation."""
    if receipt.phase == LeasePhase.RELEASED:
        return {"state": "released", "note": "原任务租约已经释放"}
    with lease_storage.readonly() as database:
        observed = lease_descendants.read(database, receipt) if database is not None else None
    if observed is None:
        return {"state": "unknown", "note": "缺少后代身份记录，保留租约"}
    outcomes = [_signal(identity) for identity in reversed(observed.descendants)]
    released = lease_runtime.reconcile(
        receipt.token, receipt.request.project, receipt.request.owner,
    )
    return {
        "state": "released" if released else "pending",
        "recorded_count": len(outcomes), "unconfirmed_count": outcomes.count(False),
        "suspected_orphan_count": len(observed.suspected_orphans),
        "note": (
            "已按确定归属记录核对停止；内核事件推定的疑似逃逸项不会自动发送信号。"
            "租约仍以退出与观察证据判断，不以信号发送成功判断"
        ),
    }
