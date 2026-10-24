"""A local Python child stands in for the native CLI; no provider/account is used."""

import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from sqlite3 import connect
from typing import Literal, assert_never
from uuid import UUID

import psutil
import pytest
from pydantic import JsonValue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli_worker
import control_bootstrap
import execution_endpoints
import flow_api
import model_registry as registry
import platform_runtime
import workflow_settings

control_bootstrap.bind_source()
from model_desk_control.host_trace_models import TaskObservation
from model_desk_control.native_events import NativeEvents
from model_desk_control.native_output import NativeReadError, OutputTail, output_tail

type CaptureReceipt = dict[str, JsonValue]


def _assert_completed_capture(
    receipts: list[CaptureReceipt], engine: Literal["claude_cli", "codex_cli"]
) -> None:
    registered = next(row for row in receipts if row.get("native_output_capture") == 1)
    assert registered["process_birth"] is not None
    assert receipts[-1]["status"] == "completed"
    match engine:
        case "claude_cli":
            assert receipts[-1]["answer"] == "fixture stream"
        case "codex_cli":
            assert "fixture stream" in str(receipts[-1]["answer"])
        case _:
            assert_never(engine)


@pytest.mark.parametrize("engine", ["claude_cli", "codex_cli"])
def test_native_stdout_is_visible_before_exit_and_stderr_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: Literal["claude_cli", "codex_cli"]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    run = project / ".model-desk/runs/task_fixture"
    run.mkdir(parents=True)
    _ = (run / "prompt.txt").write_text("Fixture-only input")
    release = run / "release.flag"
    output_relative = Path(
        ".model-desk/runs/task_fixture/native-cli/12345678123456781234567812345678/native-output.txt"
    )

    monkeypatch.setattr(cli_worker, "uuid4", lambda: UUID("12345678-1234-5678-1234-567812345678"))
    script = (
        "import sys,time;from pathlib import Path;sys.stdin.read();"
        'print(\'{"type":"assistant","message":{"content":"fixture stream"}}\',flush=True);'
        "print('fixture diagnostic',file=sys.stderr,flush=True);"
        "marker=Path(sys.argv[1]);limit=time.monotonic()+4;"
        "\nwhile not marker.exists() and time.monotonic()<limit: time.sleep(.01)\n"
        'print(\'{"type":"result","subtype":"success","is_error":false,'
        '"session_id":"fixture-session","result":"fixture stream"}\',flush=True)'
    )

    def command(
        _engine: str,
        _model: str,
        _project: Path,
        _result: Path,
        _kind: str,
        _runtime: cli_worker.CliRuntime | None = None,
    ) -> list[str]:
        return [sys.executable, "-c", script, str(release)]

    receipts: list[dict[str, JsonValue]] = []

    def record(_run: Path, **fields: JsonValue) -> None:
        receipts.append(fields)

    def gate(*_arguments: JsonValue, **_options: JsonValue) -> None:
        return None

    monkeypatch.setattr(cli_worker, "command", command)
    monkeypatch.setattr(registry, "patch_task", record)
    monkeypatch.setattr(execution_endpoints, "gate", gate)
    monkeypatch.setattr(
        workflow_settings,
        "snapshot",
        lambda: {
            "paid_allowed": True,
            "enforce_paid_budget": False,
            "enforce_model_call_limit": False,
        },
    )
    errors: list[ValueError | OSError] = []

    def execute() -> None:
        try:
            cli_worker.run_task(
                run,
                {
                    "engine": engine,
                    "kind": "chat",
                    "project": str(project),
                    "files": [],
                    "timeout": 30,
                },
                {"provider": engine, "model": "fixture-native-model"},
            )
        except (ValueError, OSError) as error:
            errors.append(error)

    worker = threading.Thread(target=execute, daemon=True)
    worker.start()
    try:
        limit = time.monotonic() + 2
        tail = output_tail(project, output_relative, "stdout")
        while time.monotonic() < limit:
            tail = output_tail(project, output_relative, "stdout")
            if "fixture stream" in tail.text:
                break
            time.sleep(0.01)
        assert worker.is_alive(), ("Child ended before live output inspection", errors)
        assert "fixture stream" in tail.text
    finally:
        _ = release.write_text("release fixture")
        worker.join(timeout=6)
    assert not worker.is_alive()
    assert errors == []
    assert (
        "fixture diagnostic" in (project / output_relative.parent / "native-errors.txt").read_text()
    )
    _assert_completed_capture(receipts, engine)
    _assert_capture_source(project, run, engine)


def test_process_birth_never_guesses_an_absent_child() -> None:
    assert platform_runtime.process_birth(2147483647) is None


def test_child_is_reaped_when_recording_its_identity_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    _ = (run / "prompt.txt").write_text("Fixture input")
    children: list[int] = []

    def record(_run: Path, **fields: JsonValue) -> None:
        pid = fields.get("process_pid")
        if isinstance(pid, int):
            children.append(pid)
            message = "Fixture identity persistence failed"
            raise cli_worker.CliRuntimeError(message)

    def command(
        _engine: str,
        _model: str,
        _project: Path,
        _result: Path,
        _kind: str,
        _runtime: cli_worker.CliRuntime | None = None,
    ) -> list[str]:
        return [sys.executable, "-c", "import sys;sys.stdin.read()"]

    def gate(*_arguments: JsonValue, **_options: JsonValue) -> None:
        return None

    monkeypatch.setattr(cli_worker, "command", command)
    monkeypatch.setattr(registry, "patch_task", record)
    monkeypatch.setattr(execution_endpoints, "gate", gate)
    monkeypatch.setattr(
        workflow_settings,
        "snapshot",
        lambda: {
            "paid_allowed": True,
            "enforce_paid_budget": False,
            "enforce_model_call_limit": False,
        },
    )
    try:
        with pytest.raises(cli_worker.CliRuntimeError, match="identity persistence failed"):
            cli_worker.run_task(
                run,
                {
                    "engine": "codex_cli",
                    "kind": "chat",
                    "project": str(tmp_path),
                    "files": [],
                    "timeout": 30,
                },
                {"provider": "codex_cli", "model": "fixture-model"},
            )
        assert len(children) == 1
        assert not psutil.pid_exists(children[0])
    finally:
        for pid in children:
            if psutil.pid_exists(pid):
                process = psutil.Process(pid)
                process.kill()
                _ = process.wait(timeout=3)


def _assert_capture_source(project: Path, run: Path, engine: str) -> None:
    capture_id = "12345678123456781234567812345678"
    record = TaskObservation(
        id="task_fixture",
        project=str(project),
        status="completed",
        engine=engine,
        native_output_capture=1,
        native_capture_id=capture_id,
    )
    _ = (run / "task.json").write_text(record.model_dump_json())
    with closing(connect(project / ".model-desk/records.db")) as database:
        _ = database.execute("CREATE TABLE tasks(id TEXT, goal_id TEXT)")
        _ = database.execute("INSERT INTO tasks VALUES ('task_fixture','')")
        database.commit()
    endpoint = "/api/flow/sessions/task:task_fixture?channel=stdout&capture_id="
    with pytest.raises(NativeReadError, match="native_capture_changed_select_session_again"):
        _ = flow_api.get(endpoint + "previous_capture", str(project))
    response = flow_api.get(endpoint + capture_id, str(project))
    assert response is not None
    assert "fixture stream" in OutputTail.model_validate_json(response).text
    projected = flow_api.get(endpoint + capture_id + "&view=events", str(project))
    assert projected is not None
    events = NativeEvents.model_validate_json(projected)
    assert events.format == "claude-stream"
    assert events.events[-1].kind == "result"
    with pytest.raises(flow_api.FlowQueryError, match="invalid_output_view"):
        _ = flow_api.get(endpoint + capture_id + "&view=html", str(project))
