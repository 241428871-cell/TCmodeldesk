"""Real host-journal and checkpoint recovery with callbacks that never call a model."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import control_bootstrap
control_bootstrap.require_graph()

import continuation_clock
import model_registry as registry
import stage_store
from goal_graph_bridge import GoalGraphHost, GraphPhaseError
from model_desk_control.host_flow import HostCallbacks, HostRef


@pytest.fixture
def reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HostRef:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(registry, "HERE", tmp_path / "desk")
    monkeypatch.setattr(registry, "REGISTRY", tmp_path / "desk/models.json")
    monkeypatch.setattr(registry, "RUNS", tmp_path / "desk/runs")
    goal = stage_store.create_goal("Goal", "Deliver an independently checked artifact", str(project), store="project")
    continuation_clock.configure_clock(str(project), goal["id"])
    _ = continuation_clock.control(str(project), goal["id"], "start")
    claim = continuation_clock.claim_due(str(project), goal["id"], "fixture-owner")
    assert claim is not None
    return HostRef(project=str(project.resolve()), goal_id=goal["id"], operation_id=claim["id"],
                   epoch=claim["authority_epoch"], owner_id=claim["owner_id"])


class SimulatedEffectLossError(RuntimeError):
    """A callback performed its effect but did not return its durable response."""


def recording_callbacks(events: list[str], *, fail_native: bool = False) -> HostCallbacks:
    def snapshot() -> str:
        events.append("snapshot")
        return '{"snapshot":true}'

    def native() -> str:
        events.append("native")
        if fail_native:
            reason = "simulated lost native response"
            raise SimulatedEffectLossError(reason)
        return '{"native":true}'

    def actions() -> str:
        events.append("actions")
        return '{"actions":true}'

    def settle() -> str:
        events.append("settle")
        return '{"settle":true}'

    return HostCallbacks(snapshot, native, actions, settle)


def test_domain_receipts_survive_lost_graph_checkpoint_without_repeating_callbacks(reference: HostRef) -> None:
    events: list[str] = []
    host = GoalGraphHost(reference, recording_callbacks(events))
    host.run()
    checkpoint = Path(reference.project) / ".model-desk/langgraph/checkpoints.sqlite"
    checkpoint.unlink()
    host.run()
    assert events == ["snapshot", "native", "actions", "settle"]


def test_unresolved_native_effect_is_not_repeated_when_graph_reenters(reference: HostRef) -> None:
    events: list[str] = []
    with pytest.raises(SimulatedEffectLossError):
        GoalGraphHost(reference, recording_callbacks(events, fail_native=True)).run()
    with pytest.raises(GraphPhaseError, match="phase_requires_original_effect_reconciliation"):
        GoalGraphHost(reference, recording_callbacks(events)).run()
    assert events == ["snapshot", "native"]


def test_forged_intent_owner_cannot_execute_callbacks(reference: HostRef) -> None:
    events: list[str] = []
    forged = reference.model_copy(update={"owner_id": "foreign-owner"})
    with pytest.raises(GraphPhaseError, match="phase_intent_owner_mismatch"):
        GoalGraphHost(forged, recording_callbacks(events)).run()
    assert events == []


def test_historical_cached_graph_result_cannot_revive_stopped_authority(reference: HostRef) -> None:
    events: list[str] = []
    host = GoalGraphHost(reference, recording_callbacks(events))
    host.run()
    stopped = continuation_clock.control(reference.project, reference.goal_id, "stop")
    host.run()
    assert events == ["snapshot", "native", "actions", "settle"]
    current = continuation_clock.snapshot(reference.project, reference.goal_id)
    assert current["authority_epoch"] == stopped["authority_epoch"]
    assert current["user_intent"] == "STOP"
