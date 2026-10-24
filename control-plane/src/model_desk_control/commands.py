"""Trusted command headers are supplied by adapters, never copied from model prose."""

from sqlite3 import Connection
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from model_desk_control.authority import read_control
from model_desk_control.control import Control, UserIntent, require_current
from model_desk_control.database import StoreError

Identity = Annotated[str, Field(min_length=1, max_length=200)]
Revision = Annotated[int, Field(ge=0)]


class GoalCommand(BaseModel):
    """A command binds one request to the authority observed by its caller."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")
    goal_id: Identity
    request_id: Identity
    epoch: Revision
    control_revision: Revision


def require_running(database: Connection, command: GoalCommand) -> Control:
    """Check current intent even if a graph checkpoint still says RUN."""
    control = read_control(database, command.goal_id)
    require_current(control, epoch=command.epoch, revision=command.control_revision)
    if control.intent is not UserIntent.RUN:
        code = "goal_not_running"
        raise StoreError(code)
    return control
