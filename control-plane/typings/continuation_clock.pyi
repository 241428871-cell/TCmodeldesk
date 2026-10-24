# The shared live-authority context used by the action adapter.
from contextlib import AbstractContextManager
from typing import Literal, TypedDict

from pydantic import JsonValue

from model_desk_control.host_effects import EffectRequest
from model_desk_control.host_settlement import SettlementRequest

class ClockError(ValueError): ...
class RequirementsChangedError(ClockError): ...

class ClockSnapshot(TypedDict):
    authority_epoch: int
    requirements_revision: int
    active_intent_id: str
    active_intent: dict[str, JsonValue] | None
    user_intent: str
    enabled: bool
    hold: dict[str, JsonValue]
    status: str

class Claim(TypedDict):
    id: str
    authority_epoch: int
    owner_id: str
    requirements_revision: int

def snapshot(project: str, goal_id: str) -> ClockSnapshot: ...
def claim_due(
    project: str,
    goal_id: str,
    owner_id: str,
    lease_seconds: int = 120,
    now: float | None = None,
    *,
    host_protocol: Literal["legacy", "graph_v2"] = "legacy",
) -> Claim | None: ...
def configure_clock(
    project: str,
    goal_id: str,
    interval_seconds: int = 1800,
    enabled: bool = False,
    obligations: list[dict[str, JsonValue]] | None = None,
    brain_model_id: str = "",
    native_session_id: str = "",
) -> ClockSnapshot: ...
def finish_round(
    project: str,
    goal_id: str,
    intent_id: str,
    epoch: int,
    result: dict[str, JsonValue],
    *,
    settlement: SettlementRequest | None = None,
) -> dict[str, JsonValue]: ...
def require_requirements_current(
    project: str,
    goal_id: str,
    intent_id: str,
    epoch: int,
    *,
    expected_revision: int | None = None,
) -> None: ...
def require_task_requirements_current(
    project: str,
    goal_id: str,
    intent_id: str,
    epoch: int,
    expected_revision: int,
) -> None: ...
def record_runner(
    project: str,
    goal_id: str,
    intent_id: str,
    epoch: int,
    pid: int,
    birth: float,
    native_session_id: str = "",
) -> dict[str, JsonValue]: ...
def control(
    project: str,
    goal_id: str,
    action: str,
    payload: dict[str, JsonValue] | None = None,
    *,
    effect: EffectRequest | None = None,
) -> ClockSnapshot: ...
def authority_guard(
    project: str, goal_id: str, epoch: int | None = None
) -> AbstractContextManager[dict[str, JsonValue]]: ...
def dispatch_guard(
    project: str, goal_id: str, stage_id: str = "", authority_epoch: int | None = None
) -> dict[str, JsonValue]: ...
