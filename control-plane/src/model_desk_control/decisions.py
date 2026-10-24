"""Human exceptions are scoped answers, not unrestricted approvals or dispatch commands."""

from enum import StrEnum
from typing import Annotated, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from model_desk_control.commands import GoalCommand, Identity, Revision
from model_desk_control.database import StoreError
from model_desk_control.plans import TaskId
from model_desk_control.task_state import Digest

Text = Annotated[str, Field(min_length=1, max_length=16000)]


class DecisionRecord(BaseModel):
    """Strict immutable public and stored boundary."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="forbid")


class ExceptionReason(StrEnum):
    """Ordinary planning and routine test failures do not require a human card."""

    MISSING_FACT = "missing_fact"
    POLICY_CONFLICT = "policy_conflict"
    AUTHORIZATION_EXPANSION = "authorization_expansion"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    EXECUTION_UNKNOWN = "execution_unknown"
    SUBJECTIVE_ACCEPTANCE = "subjective_acceptance"


class DecisionStatus(StrEnum):
    """Only a current, answered card is eligible for one task's consumption."""

    PENDING = "pending"
    ANSWERED = "answered"
    CONSUMED = "consumed"
    INVALIDATED = "invalidated"


class DecisionOption(DecisionRecord):
    """A label identifies a choice; it contains no executable shell or tool action."""

    option_id: Identity
    label: Text


class DecisionProposal(DecisionRecord):
    """The brain submits the question and evidence, never the answer or authority."""

    task_id: TaskId
    reason: ExceptionReason
    question: Text
    evidence: Annotated[tuple[Text, ...], Field(max_length=32)] = ()
    options: Annotated[tuple[DecisionOption, ...], Field(max_length=8)] = ()
    recommendation: Identity | None = None

    @model_validator(mode="after")
    def coherent_options(self) -> Self:
        """Reject ambiguous choice identities and recommendations outside the card."""
        identities = {option.option_id for option in self.options}
        if len(identities) != len(self.options) or (
            self.recommendation is not None and self.recommendation not in identities
        ):
            code = "invalid_decision_options"
            raise StoreError(code)
        return self


class DecisionScope(DecisionRecord):
    """The relevant plan content is hashed; unrelated new tasks preserve validity."""

    goal_id: Identity
    task_id: TaskId
    epoch: Revision
    plan_revision: Revision
    task_hash: Digest
    context_revision: Revision
    policy_revision: Revision
    artifact_hash: Digest | None


class HumanAnswer(DecisionRecord):
    """The HTTP adapter must supply authenticated actor identity separately from AI tools."""

    actor_id: Identity
    selected_option: Identity | None = None
    note: Annotated[str, Field(max_length=16000)] = ""
    answered_at: Revision

    @model_validator(mode="after")
    def substantive(self) -> Self:
        """A free-text-only answer must actually contain text."""
        if self.selected_option is None and not self.note.strip():
            code = "empty_human_answer"
            raise StoreError(code)
        return self


class Decision(DecisionRecord):
    """A current card and its immutable answer remain auditable after consumption."""

    decision_id: Identity
    subject_hash: Digest
    scope: DecisionScope
    proposal: DecisionProposal
    revision: Revision = 0
    status: DecisionStatus = DecisionStatus.PENDING
    answer: HumanAnswer | None = None
    invalid_reason: str | None = None

    @model_validator(mode="after")
    def coherent_record(self) -> Self:
        """Reject corrupt identities or status claims at the persisted boundary."""
        if (
            self.scope.task_id != self.proposal.task_id
            or self.decision_id != f"decision:{self.subject_hash}"
            or (
                self.status in (DecisionStatus.ANSWERED, DecisionStatus.CONSUMED)
                and self.answer is None
            )
            or (self.status is DecisionStatus.PENDING and self.answer is not None)
            or (self.status is DecisionStatus.INVALIDATED) != (self.invalid_reason is not None)
        ):
            code = "inconsistent_decision_record"
            raise StoreError(code)
        return self


class RaiseDecision(GoalCommand):
    """Only the proposal is authored by the brain."""

    proposal: DecisionProposal


class AnswerDecision(GoalCommand):
    """A user command targets exactly one current card revision."""

    decision_id: Identity
    decision_revision: Revision
    answer: HumanAnswer


class ConsumeDecision(GoalCommand):
    """One task graph consumes a previously recorded human answer."""

    decision_id: Identity
    decision_revision: Revision
    task_id: TaskId
