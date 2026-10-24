"""Bounded event-driven graph advancement; no second timer or model-based reviewer."""

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, GraphOutput

from model_desk_control.database import StoreError
from model_desk_control.decision_store import DecisionStore
from model_desk_control.decisions import DecisionStatus
from model_desk_control.task_graph_models import TaskGraphState

TaskGraph = CompiledStateGraph[TaskGraphState, None, TaskGraphState, TaskGraphState]


def advance_task(
    graph: TaskGraph,
    decisions: DecisionStore,
    state: TaskGraphState | None,
    config: RunnableConfig,
) -> GraphOutput[TaskGraphState]:
    """Resume a recorded answer automatically under the caller's per-thread execution lock.

    Call from the existing event dispatcher with a trusted thread configuration. A GET or
    import must not call this function. At most one resume occurs; no polling loop starts.
    """
    result = graph.invoke(state, config, version="v2")
    if not result.interrupts:
        return result
    if len(result.interrupts) != 1:
        code = "unexpected_task_interrupt_count"
        raise StoreError(code)
    decision_id = result.value["decision_id"]
    if decision_id is None:
        code = "task_graph_decision_missing"
        raise StoreError(code)
    decision = decisions.read(result.value["goal_id"], decision_id)
    if decision.status is not DecisionStatus.ANSWERED:
        return result
    # The graph rechecks epoch, scope and the database answer after receiving this wakeup.
    command = Command[str](resume={"decision_id": decision_id})
    return graph.invoke(command, config, version="v2")
