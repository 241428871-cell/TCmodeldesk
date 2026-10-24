"""Stage branches wait independently and recheck live facts after every restore."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from model_desk_control.host_branch import (
    BranchGate,
    BranchObservation,
    BranchRef,
    advance_branch,
    branch_identity,
    build_branch_flow,
)


@dataclass
class FakeHost:
    """Live observations are fixture state; every effect is recorded for assertions."""

    gates: dict[str, BranchObservation] = field(default_factory=dict[str, BranchObservation])
    parked: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    dispatched: list[str] = field(default_factory=list[str])
    retired: list[tuple[str, BranchGate]] = field(default_factory=list[tuple[str, BranchGate]])
    observed: list[str] = field(default_factory=list[str])
    reasons: list[str] = field(default_factory=list[str])
    fail_dispatch: bool = False

    def observe(self, reference: BranchRef) -> BranchObservation:
        self.observed.append(reference.stage_id)
        return self.gates[reference.stage_id]

    def park(self, reference: BranchRef, decision_id: str) -> None:
        self.parked.append((reference.stage_id, decision_id))

    def dispatch(self, reference: BranchRef) -> str:
        if self.fail_dispatch:
            reason = "Fixture lost the process before the original dispatch"
            raise RuntimeError(reason)
        self.dispatched.append(reference.stage_id)
        return "task-" + reference.stage_id

    def retire(self, reference: BranchRef, gate: BranchGate, reason: str) -> None:
        self.retired.append((reference.stage_id, gate))
        self.reasons.append(reason)


def ref(stage_id: str) -> BranchRef:
    return BranchRef(
        project="/project",
        goal_id="goal",
        stage_id=stage_id,
        branch_id=branch_identity("/project", "goal", stage_id, "round-1", "dispatch:0"),
        epoch=1,
        origin_operation_id="round-1",
    )


WAIT = BranchObservation(gate=BranchGate.WAITING, decision_id="decision-a")
READY = BranchObservation(gate=BranchGate.READY)


def test_compiling_never_observes_or_dispatches() -> None:
    host = FakeHost()
    with SqliteSaver.from_conn_string(":memory:") as saver:
        _ = build_branch_flow(host, saver)
    assert host.observed == []
    assert host.dispatched == []


def test_waiting_branch_does_not_hold_back_an_independent_branch(tmp_path: Path) -> None:
    host = FakeHost(gates={"a": WAIT, "b": READY})
    with SqliteSaver.from_conn_string(str(tmp_path / "branches.sqlite")) as saver:
        flow = build_branch_flow(host, saver)
        waiting = advance_branch(flow, ref("a"))
        independent = advance_branch(flow, ref("b"))
    assert waiting.waiting
    assert waiting.decision_id == "decision-a"
    assert waiting.task_id == ""
    assert not independent.waiting
    assert independent.task_id == "task-b"
    assert host.parked == [("a", "decision-a")]
    assert host.dispatched == ["b"]


def test_reopened_checkpoint_resumes_only_after_live_answer(tmp_path: Path) -> None:
    path = str(tmp_path / "branches.sqlite")
    host = FakeHost(gates={"a": WAIT})
    with SqliteSaver.from_conn_string(path) as saver:
        assert advance_branch(build_branch_flow(host, saver), ref("a")).waiting
    with SqliteSaver.from_conn_string(path) as saver:
        still = advance_branch(build_branch_flow(host, saver), ref("a"), "decision-a")
    assert still.waiting
    assert host.dispatched == []
    host.gates["a"] = READY
    with SqliteSaver.from_conn_string(path) as saver:
        resumed = advance_branch(build_branch_flow(host, saver), ref("a"), "decision-a")
    assert not resumed.waiting
    assert resumed.task_id == "task-a"
    assert host.dispatched == ["a"]


def test_restored_ready_position_rechecks_before_dispatch(tmp_path: Path) -> None:
    path = str(tmp_path / "branches.sqlite")
    host = FakeHost(gates={"a": READY}, fail_dispatch=True)
    with (
        SqliteSaver.from_conn_string(path) as saver,
        pytest.raises(RuntimeError, match="lost the process"),
    ):
        _ = advance_branch(build_branch_flow(host, saver), ref("a"))
    host.fail_dispatch = False
    host.gates["a"] = BranchObservation(gate=BranchGate.REVOKED, reason="STOP")
    with SqliteSaver.from_conn_string(path) as saver:
        outcome = advance_branch(build_branch_flow(host, saver), ref("a"))
    assert outcome.step == "closed"
    assert host.dispatched == []
    assert host.retired == [("a", BranchGate.REVOKED)]
    assert host.reasons == ["STOP"]


def test_settled_branch_closes_without_a_retire_effect(tmp_path: Path) -> None:
    host = FakeHost(gates={"a": BranchObservation(gate=BranchGate.SETTLED)})
    with SqliteSaver.from_conn_string(str(tmp_path / "branches.sqlite")) as saver:
        outcome = advance_branch(build_branch_flow(host, saver), ref("a"))
    assert outcome.step == "closed"
    assert host.retired == []
    assert host.dispatched == []


def test_lost_checkpoint_restarts_at_the_live_gate(tmp_path: Path) -> None:
    host = FakeHost(gates={"a": WAIT})
    with SqliteSaver.from_conn_string(str(tmp_path / "first.sqlite")) as saver:
        assert advance_branch(build_branch_flow(host, saver), ref("a")).waiting
    host.gates["a"] = READY
    with SqliteSaver.from_conn_string(str(tmp_path / "replacement.sqlite")) as saver:
        outcome = advance_branch(build_branch_flow(host, saver), ref("a"), "decision-a")
    assert outcome.task_id == "task-a"
    assert host.observed == ["a", "a", "a"]
