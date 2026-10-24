"""Real persistent interrupts isolate tasks and replay domain writes after a crash."""

from pathlib import Path

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from model_desk_control.database import StoreError, text_cursor
from model_desk_control.decision_store import DecisionStore
from model_desk_control.decisions import ConsumeDecision, Decision, DecisionStatus
from model_desk_control.store import ControlStore
from model_desk_control.task_driver import advance_task
from model_desk_control.task_graph import build_task_graph
from model_desk_control.task_graph_models import initial_task_state, stable_task_key
from model_desk_control.task_state import read_task
from tests.domain_fixtures import answer_for, domain_at, raise_question


def config_for(task_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": stable_task_key("project/goal", task_id, "generation-1")}}


def wake(decision: Decision) -> Command[str]:
    return Command[str](resume={"decision_id": decision.decision_id})


def test_compiling_task_graph_does_not_open_or_create_domain_repository(tmp_path: Path) -> None:
    path = tmp_path / "not-created.sqlite"
    with SqliteSaver.from_conn_string(":memory:") as saver:
        _ = build_task_graph(DecisionStore(ControlStore(path)), saver)
    assert not path.exists()


def test_one_interrupt_does_not_pause_an_independent_task_graph(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    decision = raise_question(controls)
    with SqliteSaver.from_conn_string(str(tmp_path / "graphs.sqlite")) as saver:
        graph = build_task_graph(DecisionStore(controls), saver)
        waiting = graph.invoke(
            initial_task_state(
                goal_id="project/goal", task_id="a", epoch=0, decision_id=decision.decision_id
            ),
            config_for("a"),
            version="v2",
        )
        assert waiting.value["gate_status"] == "needs_human"
        assert len(waiting.interrupts) == 1
        independent = graph.invoke(
            initial_task_state(goal_id="project/goal", task_id="b", epoch=0),
            config_for("b"),
            version="v2",
        )
        assert independent.value["gate_status"] == "ready"
        assert not independent.interrupts
    assert DecisionStore(controls).read("project/goal", decision.decision_id) == decision


def test_reopen_and_resume_consumes_only_recorded_human_answer(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    decisions = DecisionStore(controls)
    decision = raise_question(controls)
    checkpoint = str(tmp_path / "graphs.sqlite")
    with SqliteSaver.from_conn_string(checkpoint) as saver:
        _ = build_task_graph(decisions, saver).invoke(
            initial_task_state(
                goal_id="project/goal", task_id="a", epoch=0, decision_id=decision.decision_id
            ),
            config_for("a"),
            version="v2",
        )
    _ = decisions.answer(answer_for(decision))
    with SqliteSaver.from_conn_string(checkpoint) as saver:
        resumed = advance_task(build_task_graph(decisions, saver), decisions, None, config_for("a"))
        assert resumed.value["gate_status"] == "ready"
        assert resumed.value["consumed_decision_id"] == decision.decision_id
        assert not resumed.interrupts
    assert decisions.read("project/goal", decision.decision_id).status is DecisionStatus.CONSUMED


def test_recorded_answer_auto_resumes_even_before_first_task_graph_run(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    decisions = DecisionStore(controls)
    decision = raise_question(controls)
    _ = decisions.answer(answer_for(decision))
    with SqliteSaver.from_conn_string(str(tmp_path / "graphs.sqlite")) as saver:
        result = advance_task(
            build_task_graph(decisions, saver),
            decisions,
            initial_task_state(
                goal_id="project/goal", task_id="a", epoch=0, decision_id=decision.decision_id
            ),
            config_for("a"),
        )
        assert result.value["gate_status"] == "ready"
        assert not result.interrupts


def test_resume_signal_cannot_forge_an_answer(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    decisions = DecisionStore(controls)
    decision = raise_question(controls)
    with SqliteSaver.from_conn_string(str(tmp_path / "graphs.sqlite")) as saver:
        graph = build_task_graph(decisions, saver)
        _ = graph.invoke(
            initial_task_state(
                goal_id="project/goal", task_id="a", epoch=0, decision_id=decision.decision_id
            ),
            config_for("a"),
            version="v2",
        )
        with pytest.raises(StoreError, match="decision_not_answered"):
            _ = graph.invoke(wake(decision), config_for("a"), version="v2")
        assert decisions.read("project/goal", decision.decision_id).answer is None
        _ = decisions.answer(answer_for(decision))
        recovered = graph.invoke(None, config_for("a"), version="v2")
        assert recovered.value["gate_status"] == "ready"


def test_stop_overrides_a_queued_graph_resume(tmp_path: Path) -> None:
    controls = domain_at(tmp_path)
    decisions = DecisionStore(controls)
    decision = raise_question(controls)
    with SqliteSaver.from_conn_string(str(tmp_path / "graphs.sqlite")) as saver:
        graph = build_task_graph(decisions, saver)
        _ = graph.invoke(
            initial_task_state(
                goal_id="project/goal", task_id="a", epoch=0, decision_id=decision.decision_id
            ),
            config_for("a"),
            version="v2",
        )
        _ = decisions.answer(answer_for(decision))
        _ = controls.stop(goal_id="project/goal", request_id="stop")
        resumed = graph.invoke(wake(decision), config_for("a"), version="v2")
        assert resumed.value["gate_status"] == "revoked"
        assert resumed.value["consumed_decision_id"] is None
    assert decisions.read("project/goal", decision.decision_id).status is DecisionStatus.INVALIDATED


class SimulatedCheckpointLossError(RuntimeError):
    """Domain commit succeeded, but the graph node did not checkpoint its return."""


def test_domain_commit_survives_missing_graph_checkpoint_without_double_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls = domain_at(tmp_path)
    decisions = DecisionStore(controls)
    decision = raise_question(controls)
    checkpoint = str(tmp_path / "graphs.sqlite")
    with SqliteSaver.from_conn_string(checkpoint) as saver:
        _ = build_task_graph(decisions, saver).invoke(
            initial_task_state(
                goal_id="project/goal", task_id="a", epoch=0, decision_id=decision.decision_id
            ),
            config_for("a"),
            version="v2",
        )
    _ = decisions.answer(answer_for(decision))
    original_consume = DecisionStore.consume
    calls: list[ConsumeDecision] = []

    def crash_after_first_commit(store: DecisionStore, command: ConsumeDecision) -> Decision:
        result = original_consume(store, command)
        calls.append(command)
        if len(calls) == 1:
            reason = "simulated loss before graph checkpoint"
            raise SimulatedCheckpointLossError(reason)
        return result

    monkeypatch.setattr(DecisionStore, "consume", crash_after_first_commit)
    with (
        SqliteSaver.from_conn_string(checkpoint) as saver,
        pytest.raises(SimulatedCheckpointLossError),
    ):
        _ = build_task_graph(decisions, saver).invoke(wake(decision), config_for("a"), version="v2")
    with controls.connection() as database:
        revision_after_commit = read_task(database, "project/goal", "a").revision
    with SqliteSaver.from_conn_string(checkpoint) as saver:
        recovered = build_task_graph(decisions, saver).invoke(None, config_for("a"), version="v2")
        assert recovered.value["gate_status"] == "ready"
    assert len(calls) == 2
    assert calls[0] == calls[1]
    with controls.connection() as database:
        assert read_task(database, "project/goal", "a").revision == revision_after_commit
        assert text_cursor(
            database,
            """SELECT CAST(count(*) AS TEXT) FROM receipts
               WHERE request_id LIKE 'decision-consume:%'""",
        ).fetchone() == ("1",)
