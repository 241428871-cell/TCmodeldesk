"""Choose work for the existing scanner without another timer or model polling loop."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar, Final

import continuation_clock
import goal_events
import goal_resume
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

CLOCK_DATA: Final = TypeAdapter(dict[str, JsonValue])


class ScanAction(StrEnum):
    """Mutations remain in the existing round driver or terminal settlement."""

    SKIP = "skip"
    RUN = "run"
    SETTLE = "settle"


class ScanIntent(BaseModel):
    """A live lease belongs to its driver even after the goal status was committed."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    lease_until: str


class ScanClock(BaseModel):
    """Read only the current lifecycle fields needed to select a next operation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    exists: bool
    enabled: bool
    user_intent: str
    closed: bool = False
    status: str = ""
    hold: dict[str, JsonValue] = {}
    active_intent: ScanIntent | None = None
    authority_epoch: int = 0


def decide(project: str, goal_id: str, goal_status: str, driver_id: str) -> ScanAction:
    """A completed goal can still own recoverable effects; it is not yet a closed clock."""
    data = CLOCK_DATA.validate_python(continuation_clock.snapshot(project, goal_id))
    clock = ScanClock.model_validate(data)
    if not clock.exists or clock.closed:
        return ScanAction.SKIP
    if clock.active_intent is not None and not clock.hold:
        lease = datetime.fromisoformat(clock.active_intent.lease_until)
        if lease.tzinfo is None or lease > datetime.now(UTC):
            return ScanAction.SKIP
    if goal_status == "completed":
        recover = (
            clock.active_intent is not None
            and not clock.hold
            and clock.enabled
            and clock.user_intent == "RUN"
        ) or goal_resume.candidate(project, goal_id, driver_id) is not None
        return ScanAction.RUN if recover else ScanAction.SETTLE
    if goal_status != "active" or not clock.enabled or clock.user_intent != "RUN":
        return ScanAction.SKIP
    if not clock.hold and (
        clock.status in {"due", "running"}
        or goal_events.ready(project, goal_id, clock.authority_epoch)
    ):
        return ScanAction.RUN
    return (
        ScanAction.RUN
        if goal_resume.candidate(project, goal_id, driver_id) is not None
        else ScanAction.SKIP
    )
