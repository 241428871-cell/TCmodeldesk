"""Independent durable task decision gates; no executor effects run before interrupts."""

from dataclasses import dataclass
from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from model_desk_control.control import UserIntent
from model_desk_control.database import StoreError
from model_desk_control.decision_rules import invalid_reason
from model_desk_control.decision_store import DecisionStore
from model_desk_control.decisions import ConsumeDecision, Decision, DecisionStatus
from model_desk_control.plan_store import PlanStore
from model_desk_control.task_graph_models import (
    ResumeSignal,
    TaskGraphState,
    TaskGraphUpdate,
    stable_task_key,
)


def _authorized(decisions: DecisionStore, state: TaskGraphState) -> bool:
    control = decisions.controls.read(state["goal_id"])
    return (
        not control.closed
        and control.intent is UserIntent.RUN
        and not control.resource_hold
        and control.epoch == state["epoch"]
    )


def _card(decisions: DecisionStore, state: TaskGraphState) -> Decision:
    decision_id = state["decision_id"]
    if decision_id is None:
        code = "task_graph_decision_missing"
        raise StoreError(code)
    decision = decisions.read(state["goal_id"], decision_id)
    if decision.scope.task_id != state["task_id"] or decision.scope.epoch != state["epoch"]:
        code = "task_graph_decision_scope_mismatch"
        raise StoreError(code)
    return decision


def _valid(decisions: DecisionStore, decision: Decision) -> bool:
    with decisions.controls.connection() as database:
        _ = database.execute("BEGIN")
        return invalid_reason(database, decision.scope) is None


@dataclass(frozen=True, slots=True)
class TaskGate:
    """Graph node implementations share only explicit domain repositories."""

    decisions: DecisionStore

    def prepare(self, state: TaskGraphState) -> TaskGraphUpdate:
        """Observe the current task without creating a card or starting execution."""
        decisions = self.decisions
        plans = PlanStore(decisions.controls)
        if not _authorized(decisions, state):
            return {"gate_status": "revoked"}
        if state["decision_id"] is None:
            ready = state["task_id"] in plans.ready(state["goal_id"])
            return {"gate_status": "ready" if ready else "blocked"}
        decision = _card(decisions, state)
        if decision.status is DecisionStatus.INVALIDATED or not _valid(decisions, decision):
            return {"gate_status": "revoked"}
        if decision.status is DecisionStatus.CONSUMED:
            ready = state["task_id"] in plans.ready(state["goal_id"])
            return {
                "gate_status": "ready" if ready else "blocked",
                "consumed_decision_id": decision.decision_id,
            }
        return {"gate_status": "needs_human"}

    def wait_for_answer(self, state: TaskGraphState) -> TaskGraphUpdate:
        """Interrupt with a reference and verify the separately persisted answer on resume."""
        decisions = self.decisions
        if not _authorized(decisions, state):
            return {"gate_status": "revoked"}
        decision = _card(decisions, state)
        if not _valid(decisions, decision):
            return {"gate_status": "revoked"}
        # Always match this interrupt on re-entry. The payload carries no answer or authority.
        signal = interrupt(decision.decision_id, response_schema=ResumeSignal)
        if signal.decision_id != decision.decision_id:
            code = "task_graph_resume_mismatch"
            raise StoreError(code)
        if not _authorized(decisions, state):
            return {"gate_status": "revoked"}
        current = _card(decisions, state)
        if current.status is not DecisionStatus.ANSWERED or current.answer is None:
            code = "decision_not_answered"
            raise StoreError(code)
        if not _valid(decisions, current):
            return {"gate_status": "revoked"}
        control = decisions.controls.read(state["goal_id"])
        operation = stable_task_key(state["goal_id"], state["task_id"], current.decision_id)
        command = ConsumeDecision(
            goal_id=state["goal_id"],
            request_id=f"decision-consume:{operation}",
            epoch=state["epoch"],
            control_revision=control.revision,
            task_id=state["task_id"],
            decision_id=current.decision_id,
            decision_revision=current.revision,
        )
        # A checkpoint commits this exact command before the next node writes the domain DB.
        return {"gate_status": "answer_recorded", "consume_command_json": command.model_dump_json()}

    def consume_answer(self, state: TaskGraphState) -> TaskGraphUpdate:
        """Replay the checkpointed command and then recheck live authority."""
        decisions = self.decisions
        if state["gate_status"] != "answer_recorded" or not _authorized(decisions, state):
            return {"gate_status": "revoked"}
        document = state["consume_command_json"]
        if document is None:
            code = "task_graph_command_missing"
            raise StoreError(code)
        command = ConsumeDecision.model_validate_json(document)
        if (
            command.goal_id != state["goal_id"]
            or command.task_id != state["task_id"]
            or command.epoch != state["epoch"]
            or command.decision_id != state["decision_id"]
        ):
            code = "task_graph_command_scope_mismatch"
            raise StoreError(code)
        consumed = decisions.consume(command)
        if not _authorized(decisions, state) or not _valid(decisions, consumed):
            return {"gate_status": "revoked"}
        ready = state["task_id"] in PlanStore(decisions.controls).ready(state["goal_id"])
        return {
            "gate_status": "ready" if ready else "blocked",
            "consumed_decision_id": consumed.decision_id,
        }


def _route(state: TaskGraphState) -> Literal["wait", "__end__"]:
    return "wait" if state["gate_status"] == "needs_human" else "__end__"


def build_task_graph(
    decisions: DecisionStore, checkpointer: BaseCheckpointSaver[str]
) -> CompiledStateGraph[TaskGraphState, None, TaskGraphState, TaskGraphState]:
    """Compile a bounded task gate; construction never opens a database or native CLI."""
    gate = TaskGate(decisions)
    graph = StateGraph(TaskGraphState)
    _ = graph.add_node("prepare", gate.prepare)
    _ = graph.add_node("wait", gate.wait_for_answer)
    _ = graph.add_node("consume", gate.consume_answer)
    _ = graph.add_edge(START, "prepare")
    _ = graph.add_conditional_edges("prepare", _route)
    _ = graph.add_edge("wait", "consume")
    _ = graph.add_edge("consume", END)
    return graph.compile(checkpointer=checkpointer)
