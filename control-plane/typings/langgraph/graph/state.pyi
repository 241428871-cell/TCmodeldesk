# Typed v2 values invocation for the project's locked dependency surface.

from typing import Literal

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, GraphOutput, StateSnapshot

class CompiledStateGraph[StateT, ContextT, InputT, OutputT]:
    def invoke(
        self, input: InputT | Command[str] | None, config: RunnableConfig, *, version: Literal["v2"]
    ) -> GraphOutput[OutputT]: ...
    def get_state(self, config: RunnableConfig) -> StateSnapshot: ...
