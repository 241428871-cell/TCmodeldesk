# Narrow public surface used from the locked LangGraph 1.2.16 release.
#
# The upstream builder contains unbound overloads. This bridge describes only
# the supported calls; offline tests also execute the actual dependency.

from collections.abc import Callable
from typing import Final, Self

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

START: Final[str]
END: Final[str]

class StateGraph[StateT]:
    def __init__(self, state_schema: type[StateT]) -> None: ...
    def add_conditional_edges(self, source: str, path: Callable[[StateT], str]) -> Self: ...
    def add_node[UpdateT](self, name: str, action: Callable[[StateT], UpdateT]) -> Self: ...
    def add_edge(self, source: str, destination: str) -> Self: ...
    def compile(
        self, *, checkpointer: BaseCheckpointSaver[str]
    ) -> CompiledStateGraph[StateT, None, StateT, StateT]: ...
