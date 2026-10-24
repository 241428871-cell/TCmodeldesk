"""Actual project-store receipts and captured output, without providers or timers."""

import json
import os
from contextlib import closing
from pathlib import Path
from sqlite3 import connect
from typing import Literal

import pytest

from model_desk_control.host_trace import read_host_trace
from model_desk_control.host_trace_models import TaskObservation
from model_desk_control.native_output import TAIL_BYTES, NativeReadError, output_tail


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    desk = root / ".model-desk"
    desk.mkdir()
    task = desk / "runs/task_1"
    task.mkdir(parents=True)
    _ = (task / "task.json").write_text(
        json.dumps(
            {
                "id": "task_1",
                "project": str(root),
                "goal_id": "goal_1",
                "status": "completed",
                "engine": "codex_cli",
                "model_id": "fixture-model",
                "model": "fixture-native-model",
                "answer": "This content never enters the flow projection",
                "secret": "not-returned",
            }
        )
    )
    with closing(connect(desk / "records.db")) as database:
        _ = database.executescript("""
            CREATE TABLE tasks(id TEXT, goal_id TEXT);
            INSERT INTO tasks VALUES ('task_1','goal_1');
            CREATE TABLE continuation_intents(id TEXT, goal_id TEXT, data TEXT);
            CREATE TABLE goal_graph_steps(intent_id TEXT,step TEXT,status TEXT,result_json TEXT);
        """)
        _ = database.execute(
            "INSERT INTO continuation_intents VALUES ('intent_1','goal_1',?)",
            (
                json.dumps(
                    {
                        "id": "intent_1",
                        "goal_id": "goal_1",
                        "status": "finished",
                        "brain_model_id": "fixture-brain",
                        "brain_permission_mode": "auto",
                    }
                ),
            ),
        )
        snapshot = {
            "engine": "codex_cli",
            "model": "fixture-brain-model",
            "runtime": {},
            "context": {"tasks": [{"id": "task_1", "status": "completed"}]},
        }
        applied = {
            "dispatched_task_ids": ["task_1"],
            "goal_completed": False,
            "pending_obligation_ids": [],
            "summary": "done",
        }
        _ = database.executemany(
            "INSERT INTO goal_graph_steps VALUES ('intent_1',?,'done',?)",
            (
                ("snapshot", json.dumps(snapshot)),
                ("native", "{}"),
                ("actions", json.dumps(applied)),
            ),
        )
        database.commit()
    output = desk / "goal-brain/goal_1/intent_1"
    output.mkdir(parents=True)
    _ = (output / "native-output.jsonl").write_text('{"type":"turn.completed"}\n')
    return root


def test_fresh_project_flow_does_not_initialize_any_record(tmp_path: Path) -> None:
    trace = read_host_trace(tmp_path)
    assert trace.flow.nodes == ()
    assert list(tmp_path.iterdir()) == []


def test_real_receipts_create_bidirectional_messages_without_returning_content(
    project: Path,
) -> None:
    trace = read_host_trace(project)
    assert {node.session_key for node in trace.flow.nodes} == {"brain:intent_1", "task:task_1"}
    assert {(edge.source_session, edge.target_session) for edge in trace.flow.edges} == {
        ("brain:intent_1", "task:task_1"),
        ("task:task_1", "brain:intent_1"),
    }
    assert "not-returned" not in trace.model_dump_json()
    assert "This content" not in trace.model_dump_json()
    assert trace.references[1].permission_mode == "auto"


def test_inflight_actions_and_unprocessed_context_do_not_invent_messages(project: Path) -> None:
    with closing(connect(project / ".model-desk/records.db")) as database:
        _ = database.execute(
            "UPDATE goal_graph_steps SET status='inflight' WHERE step IN ('native','actions')"
        )
        database.commit()
    assert read_host_trace(project).flow.edges == ()


def test_task_record_from_another_project_stays_out_of_flow(project: Path) -> None:
    path = project / ".model-desk/runs/task_1/task.json"
    _ = path.write_text(
        json.dumps(
            {
                "id": "task_1",
                "project": "/different/project",
                "goal_id": "goal_1",
                "status": "completed",
            }
        )
    )
    trace = read_host_trace(project)
    assert len(trace.flow.nodes) == 1
    assert trace.flow.edges == ()
    assert trace.flow.unresolved_messages == 2
    assert trace.observation_gaps == 1


def test_reading_flow_and_output_preserves_database_and_native_bytes(project: Path) -> None:
    path = project / ".model-desk/goal-brain/goal_1/intent_1/native-output.jsonl"
    before = {file: file.read_bytes() for file in project.rglob("*") if file.is_file()}
    _ = read_host_trace(project)
    tail = output_tail(project, path.relative_to(project), "stdout")
    assert tail.available
    assert tail.text == '{"type":"turn.completed"}\n'
    after = {file: file.read_bytes() for file in project.rglob("*") if file.is_file()}
    assert before == after


def test_native_output_has_a_fixed_tail_budget_and_inert_controls(project: Path) -> None:
    path = project / ".model-desk/goal-brain/goal_1/intent_1/native-output.jsonl"
    _ = path.write_bytes(b"x" * (TAIL_BYTES + 120) + b"\x1b]fake-terminal-command\x07")
    tail = output_tail(project, path.relative_to(project), "stdout")
    assert tail.omitted_bytes > 120
    assert len(tail.text) <= TAIL_BYTES
    assert "\x1b" not in tail.text
    assert "fake-terminal-command" in tail.text


def test_linked_output_directory_is_rejected(project: Path, tmp_path: Path) -> None:
    link = project / ".model-desk/linked"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(NativeReadError, match="linked_record_path"):
        _ = output_tail(project, Path(".model-desk/linked/anything"), "stdout")


@pytest.mark.skipif(os.name == "nt", reason="Windows does not provide POSIX named pipes")
def test_nonregular_output_cannot_block_a_read(project: Path) -> None:
    path = project / ".model-desk/fifo"
    os.mkfifo(path)
    with pytest.raises(NativeReadError, match="non_regular_record"):
        _ = output_tail(project, path.relative_to(project), "stdout")


def test_captured_executor_uses_original_process_and_runtime_snapshot(project: Path) -> None:
    path = project / ".model-desk/runs/task_1/task.json"
    record = TaskObservation(
        id="task_1",
        project=str(project),
        goal_id="goal_1",
        status="running",
        engine="codex_cli",
        model_id="fixture-model",
        model="fixture-native-model",
        process_pid=123,
        process_birth=234.5,
        native_output_capture=1,
        sandbox_mode="disposable-project-copy",
    )
    _ = path.write_text(record.model_dump_json())
    inspected: list[tuple[int, float]] = []

    def probe(pid: int, birth: float) -> Literal["running", "exited", "unknown"]:
        inspected.append((pid, birth))
        return "running"

    trace = read_host_trace(project, probe=probe)
    node = next(item for item in trace.flow.nodes if item.session_key == "task:task_1")
    assert inspected == [(123, 234.5)]
    assert node.status == "running"
    assert not node.stale
    assert node.view_kind == "native_events"
    ref = next(item for item in trace.references if item.session_key == node.session_key)
    assert ref.permission_mode == "disposable-project-copy"
    assert ref.operation_status == "running"


def test_captured_native_tools_are_visible_as_read_only_flow_nodes(project: Path) -> None:
    path = project / ".model-desk/runs/task_1/task.json"
    record = TaskObservation(
        id="task_1",
        project=str(project),
        goal_id="goal_1",
        status="completed",
        engine="codex_cli",
        model_id="fixture-model",
        model="fixture-native-model",
        native_output_capture=1,
    )
    _ = path.write_text(record.model_dump_json())
    _ = (path.parent / "native-output.txt").write_text(
        "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": "private-thread"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "cmd_1",
                            "type": "command_execution",
                            "command": "pytest -q",
                            "exit_code": 0,
                        },
                    }
                ),
            )
        )
        + "\n"
    )
    trace = read_host_trace(project)
    activity = next(row for row in trace.flow.nodes if row.role == "external")
    assert activity.model_label == "命令 · pytest -q"
    assert activity.status == "exited"
    assert activity.view_kind == "unavailable"
    assert any(
        row.source_session == "task:task_1" and row.target_session == activity.session_key
        for row in trace.flow.edges
    )
    assert any(
        row.source_session == activity.session_key and row.target_session == "task:task_1"
        for row in trace.flow.edges
    )
    dumped = trace.model_dump_json()
    assert "private-thread" not in dumped


def test_native_journal_body_cannot_relabel_a_different_operation(project: Path) -> None:
    with closing(connect(project / ".model-desk/records.db")) as database:
        _ = database.execute(
            "UPDATE continuation_intents SET data=?",
            (json.dumps({"id": "foreign_intent", "goal_id": "goal_1", "status": "completed"}),),
        )
        database.commit()
    trace = read_host_trace(project)
    assert trace.observation_gaps == 1
    assert trace.flow.edges == ()
    assert {node.session_key for node in trace.flow.nodes} == {"task:task_1"}


def test_revoked_intent_does_not_pretend_the_original_process_exited(project: Path) -> None:
    with closing(connect(project / ".model-desk/records.db")) as database:
        _ = database.execute(
            "UPDATE continuation_intents SET data=?",
            (
                json.dumps(
                    {
                        "id": "intent_1",
                        "goal_id": "goal_1",
                        "status": "revoked",
                        "pid": 123,
                        "birth": 234.5,
                    }
                ),
            ),
        )
        database.commit()

    def probe(_pid: int, _birth: float) -> Literal["running", "exited", "unknown"]:
        return "running"

    trace = read_host_trace(project, probe=probe)
    node = next(item for item in trace.flow.nodes if item.session_key == "brain:intent_1")
    assert node.status == "running"
    ref = next(item for item in trace.references if item.session_key == node.session_key)
    assert ref.operation_status == "revoked"
