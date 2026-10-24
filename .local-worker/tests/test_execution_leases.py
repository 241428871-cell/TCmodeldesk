"""Shared executor lease contracts using isolated projects and mock worker bodies."""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import JsonValue

from model_desk_control.resource_leases import LeaseBroker, LeaseRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import execution_leases
import model_registry as registry
import panel_server as panel
import workspace_config

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

type Record = dict[str, JsonValue]

MODEL: Record = {
    "id": "m1",
    "name": "M1",
    "provider": "deepseek",
    "model": "x",
    "enabled": True,
    "api_base": "https://api.example.com/v1",
    "max_input_tokens": 32768,
    "max_output_tokens": 4096,
    "preferred_tasks": [],
    "max_task_complexity": "simple",
}


@dataclass(frozen=True, slots=True)
class LeaseFixture:
    project: Path
    models_path: Path

    def task(self, identity: str, files: tuple[str, ...] = ("a.py",)) -> Path:
        directory = registry.prepare_task_dir(identity, str(self.project))
        _ = registry.patch_task(
            directory,
            id=identity,
            project=str(self.project),
            model_id="m1",
            kind="edit",
            files=list(files),
            status="queued",
        )
        return directory

    def models(self, rows: list[Record]) -> None:
        _ = self.models_path.write_text(json.dumps({"default_model": "m1", "models": rows}))

    def concurrency(self, limit: int) -> None:
        _ = workspace_config.update_project(
            {
                "action": "concurrency",
                "path": str(self.project),
                "work_concurrency": limit,
            }
        )


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LeaseFixture]:
    root = tmp_path.resolve()
    installation = root / "install"
    installation.mkdir()
    runs = installation / "runs"
    runs.mkdir()
    monkeypatch.setattr(registry, "HERE", installation)
    monkeypatch.setattr(registry, "REGISTRY", installation / "models.json")
    monkeypatch.setattr(registry, "RUNS", runs)
    monkeypatch.setattr(execution_leases, "BROKER", LeaseBroker())
    project = root / "project"
    project.mkdir()
    result = LeaseFixture(project, installation / "models.json")
    result.models([MODEL])
    _ = workspace_config.update_project({"action": "add", "path": str(project)})
    yield result
    for owner in execution_leases.state().held:
        execution_leases.release(owner)


def wait_for(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 3
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_account_keys_follow_login_or_host_not_model_name() -> None:
    assert execution_leases.account_key({"provider": "claude_cli", "model": "a"}) == "claude_cli"
    assert execution_leases.account_key({"provider": "codex_cli", "model": "b"}) == "codex_cli"
    assert execution_leases.account_key(MODEL) == "deepseek@api.example.com"
    assert execution_leases.account_key({"provider": "local", "id": "home"}) == "local:home"


def test_only_edits_lease_files_and_unlisted_edits_lease_the_project(fixture: LeaseFixture) -> None:
    state: Record = {
        "id": "t",
        "project": str(fixture.project),
        "kind": "edit",
        "files": ["src/a.py"],
    }
    edit = execution_leases.request_for(state, MODEL)
    assert edit.files == ("src/a.py",)
    assert not edit.exclusive
    blind = execution_leases.request_for({**state, "files": []}, MODEL)
    assert blind.exclusive
    chat = execution_leases.request_for({**state, "kind": "chat", "files": ["ctx.md"]}, MODEL)
    assert (chat.files, chat.exclusive) == ((), False)


def test_project_limit_defaults_to_one_and_is_bounded(fixture: LeaseFixture) -> None:
    assert workspace_config.work_concurrency(str(fixture.project)) == 1
    fixture.concurrency(3)
    assert workspace_config.work_concurrency(str(fixture.project)) == 3
    for bad in (0, 5, True):
        with pytest.raises(workspace_config.WorkspaceError):
            _ = workspace_config.update_project(
                {
                    "action": "concurrency",
                    "path": str(fixture.project),
                    "work_concurrency": bad,
                }
            )
    saved = workspace_config.update_project({"action": "select", "path": str(fixture.project)})
    assert saved["projects"][0]["work_concurrency"] == 3


def trial(monkeypatch: pytest.MonkeyPatch) -> tuple[tuple[str, ...], int]:
    release = threading.Event()
    started: list[str] = []
    live = 0
    peak = 0
    lock = threading.Lock()

    def body(
        identity: str, *, delivery_id: str = "", lease_request: LeaseRequest | None = None
    ) -> None:
        nonlocal live, peak
        del delivery_id, lease_request
        with lock:
            started.append(identity)
            live += 1
            peak = max(peak, live)
        _ = release.wait(5)
        with lock:
            live -= 1

    monkeypatch.setattr(panel, "_execute_task", body)
    threads = [threading.Thread(target=panel.execute_task, args=(name,)) for name in ("t1", "t2")]
    threads[0].start()
    try:
        wait_for(lambda: bool(started))
        threads[1].start()
        wait_for(lambda: len(started) + len(execution_leases.state().waiting) == 2)
        snapshot = tuple(started)
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(5)
                assert not thread.is_alive()
    return snapshot, peak


def test_default_runs_workers_one_at_a_time(
    fixture: LeaseFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = fixture.task("t1")
    _ = fixture.task("t2", ("b.py",))
    snapshot, peak = trial(monkeypatch)
    assert snapshot == ("t1",)
    assert peak == 1


@pytest.mark.parametrize(("files", "expected"), [(("b.py",), 2), (("a.py",), 1)])
def test_raised_limit_respects_write_conflicts(
    fixture: LeaseFixture,
    monkeypatch: pytest.MonkeyPatch,
    files: tuple[str, ...],
    expected: int,
) -> None:
    fixture.concurrency(2)
    fixture.models([MODEL, {**MODEL, "id": "m2", "api_base": "https://other.example.com/v1"}])
    _ = fixture.task("t1")
    second = fixture.task("t2", files)
    _ = registry.patch_task(second, model_id="m2")
    snapshot, peak = trial(monkeypatch)
    assert len(snapshot) == expected
    assert peak == expected


def test_cancelled_waiter_releases_without_launching(
    fixture: LeaseFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = fixture.task("t1")
    second = fixture.task("t2", ("b.py",))
    request = execution_leases.request_for(registry.read_task(first), MODEL)
    execution_leases.acquire(request, first)
    calls: list[str] = []

    def body(
        identity: str, *, delivery_id: str = "", lease_request: LeaseRequest | None = None,
        admission_error: str = "",
    ) -> None:
        del delivery_id, lease_request, admission_error
        if not (registry.task_dir(identity) / "cancelled.flag").exists():
            calls.append(identity)

    monkeypatch.setattr(panel, "_execute_task", body)
    worker = threading.Thread(target=panel.execute_task, args=("t2",))
    worker.start()
    try:
        wait_for(lambda: bool(execution_leases.state().waiting))
        _ = (second / "cancelled.flag").write_text("")
        worker.join(3)
        assert not worker.is_alive()
        assert calls == []
        assert execution_leases.state().waiting == ()
        assert execution_leases.state().held == ("t1",)
    finally:
        execution_leases.release(request.owner)
        worker.join(3)
