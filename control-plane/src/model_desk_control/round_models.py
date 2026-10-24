"""Durable planning operation identity and trusted native-runner observations."""

from enum import StrEnum
from typing import Annotated, ClassVar, Self, assert_never

from pydantic import BaseModel, ConfigDict, Field, model_validator

from model_desk_control.commands import Identity, Revision
from model_desk_control.control import Control
from model_desk_control.database import StoreError
from model_desk_control.plans import Plan, PlanPatch

OperationId = Annotated[str, Field(min_length=1, max_length=512)]


class RoundRecord(BaseModel):
    """Strict persisted boundary shared by host-neutral controller and adapters."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")


class FeedbackKind(StrEnum):
    """NOT_STARTED requires an adapter receipt proving no native prompt was sent."""

    RUNNING = "running"
    UNKNOWN = "unknown"
    RESULT = "result"
    FAILED = "failed"
    NOT_STARTED = "not_started"


class RoundStage(StrEnum):
    """Only PREPARED may authorize a first start; uncertainty permits inspection only."""

    PREPARED = "prepared"
    IN_FLIGHT = "in_flight"
    UNKNOWN = "unknown"
    RESULT = "result"
    FAILED = "failed"
    NOT_STARTED = "not_started"
    FINALIZED = "finalized"


class RoundDisposition(StrEnum):
    """A completed planning round does not imply the user's goal is complete."""

    APPLIED = "applied"
    REVOKED = "revoked"
    REPLAN_REQUIRED = "replan_required"
    FAILED = "failed"
    NOT_STARTED = "not_started"


class RoundAction(StrEnum):
    """The transactional arm operation selects at most one START caller."""

    START = "start"
    INSPECT = "inspect"
    NONE = "none"
    REVOKED = "revoked"


class RoundRequest(RoundRecord):
    """Immutable input captured before the first native-runner call."""

    operation_id: OperationId
    control: Control
    plan: Plan


class BrainFeedback(RoundRecord):
    """Only a trusted runner can attest these facts; model prose is never such a receipt."""

    operation_id: OperationId
    receipt_id: Identity
    sequence: Annotated[int, Field(gt=0)]
    kind: FeedbackKind
    evidence_ref: Identity
    patch: PlanPatch | None = None
    reason: Annotated[str, Field(min_length=1, max_length=2000)] | None = None

    @property
    def terminal(self) -> bool:
        """Whether this receipt proves there is no active native planning turn."""
        return self.kind in (FeedbackKind.RESULT, FeedbackKind.FAILED, FeedbackKind.NOT_STARTED)

    @model_validator(mode="after")
    def coherent_payload(self) -> Self:
        """A result needs a patch; uncertainty and failure require an explicit reason."""
        if (self.kind is FeedbackKind.RESULT) != (self.patch is not None) or (
            self.kind in (FeedbackKind.UNKNOWN, FeedbackKind.FAILED, FeedbackKind.NOT_STARTED)
            and self.reason is None
        ):
            code = "inconsistent_brain_feedback"
            raise StoreError(code)
        return self


class PlanningRound(RoundRecord):
    """Domain authority for one continuation operation, independent of checkpoint survival."""

    request: RoundRequest
    revision: Revision = 0
    stage: RoundStage = RoundStage.PREPARED
    armed: bool = False
    feedback: BrainFeedback | None = None
    disposition: RoundDisposition | None = None
    committed_plan_revision: Revision | None = None
    rejection_code: str | None = None

    @model_validator(mode="after")
    def coherent_state(self) -> Self:
        """Reject recovered launch or completion claims without their required evidence."""
        feedback = self.feedback
        match self.stage:
            case RoundStage.PREPARED:
                coherent = not self.armed and feedback is None
            case RoundStage.IN_FLIGHT:
                coherent = self.armed and (
                    feedback is None or feedback.kind is FeedbackKind.RUNNING
                )
            case (
                RoundStage.UNKNOWN | RoundStage.RESULT | RoundStage.FAILED | RoundStage.NOT_STARTED
            ):
                expected = {
                    RoundStage.UNKNOWN: FeedbackKind.UNKNOWN,
                    RoundStage.RESULT: FeedbackKind.RESULT,
                    RoundStage.FAILED: FeedbackKind.FAILED,
                    RoundStage.NOT_STARTED: FeedbackKind.NOT_STARTED,
                }
                coherent = (
                    self.armed and feedback is not None and feedback.kind is expected[self.stage]
                )
            case RoundStage.FINALIZED:
                coherent = self.disposition is not None and (
                    (self.armed and feedback is not None and feedback.terminal)
                    or (
                        not self.armed
                        and feedback is None
                        and self.disposition is RoundDisposition.REVOKED
                    )
                )
            case _:
                assert_never(self.stage)
        if (
            not coherent
            or (feedback is not None and feedback.operation_id != self.request.operation_id)
            or (self.stage is not RoundStage.FINALIZED and self.disposition is not None)
            or (
                (self.disposition is RoundDisposition.APPLIED)
                != (self.committed_plan_revision is not None)
            )
        ):
            code = "inconsistent_planning_round"
            raise StoreError(code)
        return self
