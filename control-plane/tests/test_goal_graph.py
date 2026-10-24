"""Real LangGraph checkpoints coordinate domain state and a separately held fake runner."""

from pathlib import Path
from typing import assert_never

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver

from model_desk_control.authority import read_control, write_control
from model_desk_control.control import UserIntent, pause, set_resource_hold, stop
from model_desk_control.goal_graph import GoalNodes, build_goal_graph, initial_goal_state
from model_desk_control.plan_store import PlanStore
from model_desk_control.round_models import FeedbackKind, RoundDisposition, RoundStage
from model_desk_control.round_store import RoundStore
from model_desk_control.store import ControlStore
from tests.round_fixtures import (
    FixedClock,
    LedgerRunner,
    SimulatedTransportLossError,
    feedback_for,
    leased_goal,
)


def round_config() -> RunnableConfig:
    return {"configurable": {"thread_id": "goal/continuation-1"}}


def test_compiling_goal_graph_does_not_create_domain_or_call_runner(tmp_path: Path) -> None:
    path = tmp_path / "absent.sqlite"
    runner = LedgerRunner()
    with SqliteSaver.from_conn_string(":memory:") as saver:
        _ = build_goal_graph(GoalNodes(RoundStore(ControlStore(path)), runner, FixedClock()), saver)
    assert not path.exists()
    assert runner.starts == []
    assert runner.inspections == []


def test_goal_graph_commits_dynamic_plan_and_settles_continuation(tmp_path: Path) -> None:
    controls, lease = leased_goal(tmp_path)
    runner = LedgerRunner()
    rounds = RoundStore(controls)
    with SqliteSaver.from_conn_string(str(tmp_path / "graphs.sqlite")) as saver:
        result = build_goal_graph(GoalNodes(rounds, runner, FixedClock()), saver).invoke(
            initial_goal_state(lease), round_config(), version="v2"
        )
    assert result.value["phase"] == "finalized"
    assert len(runner.starts) == 1
    assert runner.inspections == []
    assert PlanStore(controls).read(lease.goal_id).revision == 2
    assert controls.read(lease.goal_id).due_at == 1900
    assert not controls.read(lease.goal_id).busy


def test_lost_native_reply_reopens_checkpoint_and_inspects_without_second_start(
    tmp_path: Path,
) -> None:
    controls, lease = leased_goal(tmp_path)
    runner = LedgerRunner()
    runner.lose_first_reply = True
    rounds = RoundStore(controls)
    checkpoint = str(tmp_path / "graphs.sqlite")
    with (
        SqliteSaver.from_conn_string(checkpoint) as saver,
        pytest.raises(SimulatedTransportLossError),
    ):
        _ = build_goal_graph(GoalNodes(rounds, runner, FixedClock()), saver).invoke(
            initial_goal_state(lease), round_config(), version="v2"
        )
    assert rounds.read(lease.goal_id, lease.operation_id).stage is RoundStage.IN_FLIGHT
    with SqliteSaver.from_conn_string(checkpoint) as saver:
        result = build_goal_graph(GoalNodes(rounds, runner, FixedClock(131)), saver).invoke(
            None, round_config(), version="v2"
        )
    assert result.value["phase"] == "finalized"
    assert len(runner.starts) == 1
    assert runner.inspections == runner.starts
    assert not controls.read(lease.goal_id).busy


def test_lost_checkpoint_rebuilds_from_domain_without_restarting_native_round(
    tmp_path: Path,
) -> None:
    controls, lease = leased_goal(tmp_path)
    runner = LedgerRunner()
    rounds = RoundStore(controls)
    with SqliteSaver.from_conn_string(str(tmp_path / "first.sqlite")) as saver:
        _ = build_goal_graph(GoalNodes(rounds, runner, FixedClock()), saver).invoke(
            initial_goal_state(lease), round_config(), version="v2"
        )
    expected = controls.read(lease.goal_id)
    with SqliteSaver.from_conn_string(str(tmp_path / "replacement.sqlite")) as saver:
        rebuilt = build_goal_graph(GoalNodes(rounds, runner, FixedClock(200)), saver).invoke(
            initial_goal_state(lease), round_config(), version="v2"
        )
    assert rebuilt.value["phase"] == "finalized"
    assert len(runner.starts) == 1
    assert runner.inspections == []
    assert controls.read(lease.goal_id) == expected
    assert PlanStore(controls).read(lease.goal_id).revision == 2


def test_running_round_advances_on_later_observation_without_new_prompt(tmp_path: Path) -> None:
    controls, lease = leased_goal(tmp_path)
    runner = LedgerRunner(immediate_result=False)
    rounds = RoundStore(controls)
    with SqliteSaver.from_conn_string(str(tmp_path / "graphs.sqlite")) as saver:
        waiting = build_goal_graph(GoalNodes(rounds, runner, FixedClock()), saver).invoke(
            initial_goal_state(lease), round_config(), version="v2"
        )
        assert waiting.value["phase"] == "waiting"
        assert controls.read(lease.goal_id).busy
        request = runner.starts[0]
        runner.ledger[lease.operation_id] = feedback_for(request, FeedbackKind.RESULT, 2)
        completed = build_goal_graph(GoalNodes(rounds, runner, FixedClock(131)), saver).invoke(
            initial_goal_state(lease), round_config(), version="v2"
        )
    assert completed.value["phase"] == "finalized"
    assert len(runner.starts) == 1
    assert len(runner.inspections) == 1
    assert not controls.read(lease.goal_id).busy


def test_stop_during_native_round_archives_result_without_new_plan_or_revival(
    tmp_path: Path,
) -> None:
    controls, lease = leased_goal(tmp_path)
    runner = LedgerRunner()

    def stop_during_start() -> None:
        _ = controls.stop(goal_id=lease.goal_id, request_id="stop-during-turn")

    runner.on_start = stop_during_start
    rounds = RoundStore(controls)
    with SqliteSaver.from_conn_string(str(tmp_path / "graphs.sqlite")) as saver:
        result = build_goal_graph(GoalNodes(rounds, runner, FixedClock()), saver).invoke(
            initial_goal_state(lease), round_config(), version="v2"
        )
    assert result.value["phase"] == "finalized"
    assert rounds.read(lease.goal_id, lease.operation_id).disposition is RoundDisposition.REVOKED
    assert PlanStore(controls).read(lease.goal_id).revision == 1
    state = controls.read(lease.goal_id)
    assert state.intent is UserIntent.STOP
    assert state.due_at is None
    assert not state.busy
    assert len(runner.starts) == 1


def test_unknown_runner_ledger_remains_unknown_instead_of_assuming_not_started(
    tmp_path: Path,
) -> None:
    controls, lease = leased_goal(tmp_path)
    runner = LedgerRunner()
    rounds = RoundStore(controls)
    assert rounds.prepare(lease, now=100) is not None
    _ = rounds.choose(lease, now=100)
    with SqliteSaver.from_conn_string(str(tmp_path / "graphs.sqlite")) as saver:
        result = build_goal_graph(GoalNodes(rounds, runner, FixedClock(131)), saver).invoke(
            initial_goal_state(lease), round_config(), version="v2"
        )
    assert result.value["phase"] == "waiting"
    assert runner.starts == []
    assert len(runner.inspections) == 1
    assert rounds.read(lease.goal_id, lease.operation_id).stage is RoundStage.UNKNOWN
    assert controls.read(lease.goal_id).busy


@pytest.mark.parametrize("intent", [UserIntent.RUN, UserIntent.PAUSE, UserIntent.STOP])
def test_current_domain_stop_pause_or_resource_hold_prevents_native_start(
    tmp_path: Path, intent: UserIntent
) -> None:
    controls, lease = leased_goal(tmp_path)
    with controls.connection() as database, database:
        prior = read_control(database, lease.goal_id)
        match intent:
            case UserIntent.RUN:
                changed = set_resource_hold(prior, held=True)
            case UserIntent.PAUSE:
                changed = pause(prior)
            case UserIntent.STOP:
                changed = stop(prior)
            case _:
                assert_never(intent)
        write_control(database, prior, changed)
    runner = LedgerRunner()
    with SqliteSaver.from_conn_string(str(tmp_path / "graphs.sqlite")) as saver:
        result = build_goal_graph(
            GoalNodes(RoundStore(controls), runner, FixedClock()), saver
        ).invoke(initial_goal_state(lease), round_config(), version="v2")
    assert result.value["phase"] == "revoked"
    assert runner.starts == []
    assert runner.inspections == []
