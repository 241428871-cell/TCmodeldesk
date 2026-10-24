"""Goal recovery distinguishes domain completion, graph completion and unlaunched work."""

from pathlib import Path
from typing import override

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from model_desk_control.control import UserIntent
from model_desk_control.database import StoreError
from model_desk_control.goal_graph import GoalNodes, build_goal_graph, initial_goal_state
from model_desk_control.outbox import ContinuationLease
from model_desk_control.plan_store import PlanStore
from model_desk_control.round_models import BrainFeedback, FeedbackKind, PlanningRound, RoundRequest
from model_desk_control.round_store import RoundStore
from model_desk_control.store import ControlStore
from tests.round_fixtures import FixedClock, LedgerRunner, leased_goal
from tests.test_goal_graph import round_config


class SimulatedGoalCheckpointLossError(RuntimeError):
    """The entire domain transaction succeeded but graph checkpointing did not."""


def test_domain_settlement_before_checkpoint_is_replayed_without_plan_or_native_duplication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controls, lease = leased_goal(tmp_path)
    rounds = RoundStore(controls)
    runner = LedgerRunner()
    original = RoundStore.finalize
    calls: list[ContinuationLease] = []

    def lose_checkpoint(store: RoundStore, current: ContinuationLease) -> PlanningRound:
        result = original(store, current)
        calls.append(current)
        if len(calls) == 1:
            reason = "lost goal checkpoint after domain settlement"
            raise SimulatedGoalCheckpointLossError(reason)
        return result

    monkeypatch.setattr(RoundStore, "finalize", lose_checkpoint)
    checkpoint = str(tmp_path / "graphs.sqlite")
    with (
        SqliteSaver.from_conn_string(checkpoint) as saver,
        pytest.raises(SimulatedGoalCheckpointLossError),
    ):
        _ = build_goal_graph(GoalNodes(rounds, runner, FixedClock()), saver).invoke(
            initial_goal_state(lease), round_config(), version="v2"
        )
    committed = controls.read(lease.goal_id)
    with SqliteSaver.from_conn_string(checkpoint) as saver:
        result = build_goal_graph(GoalNodes(rounds, runner, FixedClock(131)), saver).invoke(
            None, round_config(), version="v2"
        )
    assert result.value["phase"] == "finalized"
    assert len(runner.starts) == 1
    assert runner.inspections == []
    assert controls.read(lease.goal_id) == committed
    assert PlanStore(controls).read(lease.goal_id).revision == 2


class StopBeforeHandoffClock:
    """Mutable deterministic clock injects user STOP at the exact third authority check."""

    def __init__(self, controls: ControlStore) -> None:
        self.controls: ControlStore = controls
        self.calls: int = 0

    def now(self) -> int:
        self.calls += 1
        if self.calls == 3:
            _ = self.controls.stop(goal_id="project/goal", request_id="stop-before-handoff")
        return 100


def test_stop_between_domain_arm_and_native_handoff_records_positive_unsent_proof(
    tmp_path: Path,
) -> None:
    controls, lease = leased_goal(tmp_path)
    runner = LedgerRunner()
    rounds = RoundStore(controls)
    with SqliteSaver.from_conn_string(str(tmp_path / "graphs.sqlite")) as saver:
        result = build_goal_graph(
            GoalNodes(rounds, runner, StopBeforeHandoffClock(controls)), saver
        ).invoke(initial_goal_state(lease), round_config(), version="v2")
    assert result.value["phase"] == "finalized"
    assert runner.starts == []
    assert runner.inspections == []
    feedback = rounds.read(lease.goal_id, lease.operation_id).feedback
    assert feedback is not None
    assert feedback.kind is FeedbackKind.NOT_STARTED
    state = controls.read(lease.goal_id)
    assert state.intent is UserIntent.STOP
    assert state.due_at is None
    assert not state.busy


class WrongOperationRunner(LedgerRunner):
    """A bad adapter mislabels an otherwise accepted native response."""

    @override
    def start_round(self, request: RoundRequest) -> BrainFeedback:
        return super().start_round(request).model_copy(update={"operation_id": "other-operation"})


def test_adapter_response_cannot_be_attached_to_a_different_operation(tmp_path: Path) -> None:
    controls, lease = leased_goal(tmp_path)
    rounds = RoundStore(controls)
    with (
        SqliteSaver.from_conn_string(str(tmp_path / "graphs.sqlite")) as saver,
        pytest.raises(StoreError, match="runner_operation_mismatch"),
    ):
        _ = build_goal_graph(GoalNodes(rounds, WrongOperationRunner(), FixedClock()), saver).invoke(
            initial_goal_state(lease), round_config(), version="v2"
        )
    assert rounds.read(lease.goal_id, lease.operation_id).feedback is None
    assert controls.read(lease.goal_id).busy
