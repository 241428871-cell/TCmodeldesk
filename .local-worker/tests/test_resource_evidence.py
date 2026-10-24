"""Resource inspection is scoped and never performs recovery or dispatch."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lease_descendants
import lease_runtime
import lease_storage
import model_registry as registry
import resource_evidence
from test_cli_writeback_recovery import saved as saved
from test_goal_action_receipts import run as run
from test_goal_resolution_http import Browser
from test_goal_resolution_http import browser as browser

from model_desk_control import durable_leases as leases
from model_desk_control.host_queue_models import Process
from model_desk_control.resource_leases import LeaseRequest


def test_inspection_keeps_original_files_and_receipts(saved: Path) -> None:
    before = (saved / "task.json").read_bytes()
    state = registry.read_task(saved)
    report = resource_evidence.describe(saved)
    assert report["writeback"] == {
        "state": "verified", "label": "已核对保存回执与文件版本",
        "files": [{"name": "generated.txt", "state": "pending"}],
        "test_recorded": False,
        "note": "仅恢复保存的文件，不调用模型；执行时仍重新核对授权、租约和文件。",
    }
    assert not (Path(str(state["project"])) / "generated.txt").exists()
    assert (saved / "task.json").read_bytes() == before
    assert not (registry.HERE / "resource-leases.db").exists()


@pytest.mark.parametrize("content", ["human edit", "saved output"])
def test_preview_distinguishes_conflict_and_already_applied(saved: Path, content: str) -> None:
    state = registry.read_task(saved)
    target = Path(str(state["project"])) / "generated.txt"
    _ = target.write_text(content)
    report = resource_evidence.describe(saved)
    writeback = report["writeback"]
    assert isinstance(writeback, dict)
    assert writeback["files"] == [{
        "name": "generated.txt", "state": "applied" if content == "saved output" else "conflict",
    }]
    assert target.read_text() == content


def test_damaged_candidate_does_not_look_recoverable(saved: Path) -> None:
    _ = (saved / "native-cli/fixture-capture/proposed-files/generated.txt").write_text("tampered")
    report = resource_evidence.describe(saved)
    writeback = report["writeback"]
    assert isinstance(writeback, dict)
    assert writeback["state"] == "unknown"


def test_cancelled_candidates_are_retained_not_pending_writeback(saved: Path) -> None:
    (saved / "cancelled.flag").touch()
    report = resource_evidence.describe(saved)
    writeback = report["writeback"]
    assert isinstance(writeback, dict)
    assert writeback["state"] == "retained"
    assert writeback["files"] == [{"name": "generated.txt", "state": "retained"}]
    candidate = saved / "native-cli/fixture-capture/proposed-files/generated.txt"
    assert candidate.read_text() == "saved output"


def test_other_task_lease_is_not_exposed_or_released(saved: Path) -> None:
    state = registry.read_task(saved)
    lease = lease_runtime.reserve(LeaseRequest(
        owner="other-task", project=str(state["project"]), account="fixture", files=("a.py",),
    ))
    assert lease is not None
    _ = registry.patch_task(saved, execution_lease_token=lease.token, execution_lease_protocol=1)
    report = resource_evidence.describe(saved)
    assert report["lease"] == {"state": "unknown", "label": "租约与当前任务不匹配"}
    assert lease_runtime.current(lease.token) == lease


def test_http_resource_report_uses_original_project_scope(saved: Path, browser: Browser) -> None:
    path = f"/api/tasks/{saved.name}/resources"
    reply = browser.request(path)
    assert reply.status == 200
    assert reply.body["task_id"] == saved.name
    other = saved.parents[3] / "other-project"
    other.mkdir()
    rejected = browser.request(path, project=str(other))
    assert rejected.status == 403
    assert "该记录不属于当前项目" in str(rejected.body["error"])


def test_report_exposes_fork_guard_without_process_identities(
    saved: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = registry.read_task(saved)
    receipt = lease_runtime.reserve(LeaseRequest(
        owner=str(state["id"]), project=str(state["project"]), account="fixture", files=("a.py",),
    ))
    assert receipt is not None
    lease_runtime.arm(receipt.token)
    launcher = Process(pid=2147483647, birth=1.0)
    candidate = Process(pid=2147483646, birth=2.0)
    with lease_storage.transaction() as database:
        receipt = leases.started(
            database, lease_runtime.current(receipt.token), launcher, containment="posix_session",
        )
        lease_descendants.initialize(database, receipt.token, launcher)
        observed = lease_descendants.Observation(
            token=receipt.token,
            launcher=launcher,
            state="closed",
            fork_guard="darwin_kqueue",
            suspected_orphans=(candidate,),
            uncertainty_reason="suspected_orphan",
        )
        _ = database.execute(
            "UPDATE lease_descendants SET data=? WHERE token=?",
            (observed.model_dump_json(), receipt.token),
        )
    _ = registry.patch_task(
        saved, execution_lease_token=receipt.token, execution_lease_protocol=1,
    )
    monkeypatch.setattr(resource_evidence.lease_processes, "exited", lambda _item: True)
    monkeypatch.setattr(resource_evidence.lease_processes, "quiet", lambda _item: False)
    lease = resource_evidence.describe(saved)["lease"]
    assert isinstance(lease, dict)
    assert lease["fork_guard"] == "darwin_kqueue"
    assert lease["descendant_count"] == 0
    assert lease["suspected_orphan_count"] == 1
    assert "2147483646" not in str(lease)
