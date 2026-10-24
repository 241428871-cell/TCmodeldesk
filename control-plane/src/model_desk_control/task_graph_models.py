"""Small task-graph references; human answers remain in the domain repository."""

from hashlib import sha256
from json import dumps
from typing import Literal, TypedDict

from model_desk_control.commands import Identity
from model_desk_control.decisions import DecisionRecord

GateStatus = Literal["pending", "needs_human", "answer_recorded", "ready", "blocked", "revoked"]


class ResumeSignal(DecisionRecord):
    """A wakeup references an answer; supplying this signal cannot invent one."""

    decision_id: Identity


class TaskGraphState(TypedDict):
    """Checkpointed observations are not a runner authorization token."""

    goal_id: str
    task_id: str
    epoch: int
    decision_id: str | None
    gate_status: GateStatus
    consume_command_json: str | None
    consumed_decision_id: str | None


class TaskGraphUpdate(TypedDict, total=False):
    """Partial node updates leave trusted identity unchanged."""

    gate_status: GateStatus
    consume_command_json: str | None
    consumed_decision_id: str | None


def stable_task_key(goal_id: str, task_id: str, generation: str) -> str:
    """Encode identities without ambiguous separators or path-dependent thread names."""
    encoded = dumps((goal_id, task_id, generation), ensure_ascii=False, separators=(",", ":"))
    return sha256(encoded.encode()).hexdigest()


def initial_task_state(
    *, goal_id: str, task_id: str, epoch: int, decision_id: str | None = None
) -> TaskGraphState:
    """Create a compact input; adapters must validate its project and goal ownership."""
    return {
        "goal_id": goal_id,
        "task_id": task_id,
        "epoch": epoch,
        "decision_id": decision_id,
        "gate_status": "pending",
        "consume_command_json": None,
        "consumed_decision_id": None,
    }
