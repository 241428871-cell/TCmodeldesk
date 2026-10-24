"""A child must check the lease using the model and paths it will actually use."""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import pytest
from pydantic import JsonValue, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import execution_endpoints
import execution_leases
import model_registry as registry
import panel_server as panel
import run_worker
import workflow_settings
from test_execution_leases import MODEL, LeaseFixture
from test_execution_leases import fixture as fixture

if TYPE_CHECKING:
    from collections.abc import Callable

type Record = dict[str, JsonValue]


@runtime_checkable
class Worker(Protocol):
    def run_task(self, run: Path, state: Record, model: Record) -> int | None: ...


class ChatWorker(Protocol):
    def run_chat(self, run: Path, state: Record, model: Record) -> None: ...


class MainWorker(Protocol):
    def main(self) -> int: ...


def entrypoint(name: str) -> Callable[[Path, Record, Record], int | None]:
    if name == "direct":
        return chat_adapter().run_chat
    worker = import_module(name + "_worker")
    assert isinstance(worker, Worker)
    return worker.run_task


def chat_adapter() -> ChatWorker:
    return panel


def stamped(fixture: LeaseFixture) -> tuple[Path, Record]:
    run = fixture.task("identity")
    state = registry.read_task(run)
    state["execution_lease"] = execution_leases.request_for(state, MODEL).model_dump_json()
    return run, state


@pytest.mark.parametrize("worker", ["cli", "dsh", "pi", "workbuddy", "direct"])
def test_each_child_refuses_changed_account_before_any_provider_access(
    fixture: LeaseFixture, worker: str
) -> None:
    run, state = stamped(fixture)
    state["engine"] = "pi" if worker == "pi" else worker
    model: Record = {**MODEL, "api_base": "https://changed.example.com/v1"}
    with pytest.raises(execution_leases.LeaseChangedError, match="资源身份"):
        _ = entrypoint(worker)(run, state, model)


def test_aider_validates_its_actual_command_paths(
    fixture: LeaseFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, state = stamped(fixture)
    model: Record = {**MODEL, "provider": "local", "api_key": "", "bypass_proxy": False}
    snapshot = execution_leases.request_for(state, model).model_dump_json()
    _ = registry.patch_task(run, execution_lease=snapshot)

    def get_model(*_args: JsonValue, **_kwargs: JsonValue) -> Record:
        return model

    def gate(*_args: JsonValue) -> None:
        return None

    monkeypatch.setattr(run_worker, "get_model", get_model)
    monkeypatch.setattr(execution_endpoints, "gate", gate)
    monkeypatch.setattr(workflow_settings, "snapshot", dict)
    monkeypatch.setattr(
        sys, "argv", ["run_worker", "--model-id", "m1", "--run-id", "identity",
        "--project", str(fixture.project), "--files", "b.py", "--task", "fixture"]
    )
    worker: MainWorker = run_worker
    with pytest.raises(execution_leases.LeaseChangedError, match="资源身份"):
        _ = worker.main()


@pytest.mark.parametrize("replacement", [{"files": ["b.py"]}, {"kind": "chat"}, {"id": "other"}])
def test_task_identity_changes_are_refused(fixture: LeaseFixture, replacement: Record) -> None:
    _, state = stamped(fixture)
    with pytest.raises(execution_leases.LeaseChangedError):
        execution_leases.validate_task({**state, **replacement}, MODEL)


def test_live_limit_change_and_alias_retarget_are_refused(fixture: LeaseFixture) -> None:
    (fixture.project / "a.py").symlink_to(fixture.project / "before.py")
    _, state = stamped(fixture)
    execution_leases.validate_task(state, MODEL)
    (fixture.project / "a.py").unlink()
    (fixture.project / "a.py").symlink_to(fixture.project / "after.py")
    with pytest.raises(execution_leases.LeaseChangedError):
        execution_leases.validate_task(state, MODEL)
    (fixture.project / "a.py").unlink()
    (fixture.project / "a.py").symlink_to(fixture.project / "before.py")
    fixture.concurrency(2)
    with pytest.raises(execution_leases.LeaseChangedError):
        execution_leases.validate_task(state, MODEL)


@pytest.mark.parametrize("broken", [None, 0, {}, "invalid"])
def test_malformed_snapshot_never_downgrades_to_legacy(
    fixture: LeaseFixture, broken: JsonValue
) -> None:
    _, state = stamped(fixture)
    with pytest.raises((execution_leases.LeaseChangedError, ValidationError)):
        execution_leases.validate_task({**state, "execution_lease": broken}, MODEL)


def test_legacy_and_unchanged_inputs_keep_original_admission(fixture: LeaseFixture) -> None:
    _, state = stamped(fixture)
    execution_leases.validate_task(state, MODEL)
    del state["execution_lease"]
    execution_leases.validate_task(state, MODEL)
