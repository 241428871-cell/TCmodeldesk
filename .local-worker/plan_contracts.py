"""Typed request boundaries for AI plans and explicit human decisions."""
from __future__ import annotations

from typing import Final, Literal, NewType, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

GoalId = NewType('GoalId', str)
StageId = NewType('StageId', str)
DecisionId = NewType('DecisionId', str)
MAX_STAGES: Final = 200


class PlanError(ValueError):
    """A plan or decision violates one named governance constraint."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


class Boundary(BaseModel):
    """Reject unknown fields rather than silently accepting changed authority."""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)


class GoalContract(Boundary):
    criteria: list[str] = Field(min_length=1, max_length=40)
    review_policy: Literal['exceptions', 'final'] = 'exceptions'
    allowed_files: list[str] | None = None
    file_scope: Literal['project', 'selected'] | None = None
    base_revision: int | None = Field(default=None, ge=0)

    @field_validator('criteria')
    @classmethod
    def clean_criteria(cls, values: list[str]) -> list[str]:
        """Require independently checkable, nonempty human criteria."""
        if any(not value.strip() or len(value) > 2000 for value in values):
            raise PlanError('criteria', '验收准则不能为空且每条最多 2000 字')
        return [value.strip() for value in values]

    @model_validator(mode='after')
    def explicit_file_scope(self) -> Self:
        """A project-wide authorization is not presented as a finite whitelist."""
        if self.file_scope == 'project' and self.allowed_files:
            raise PlanError('file_scope', '项目范围不使用固定文件清单；如需限制文件请选择指定范围')
        return self


class StageSpec(Boundary):
    key: str = Field(min_length=1, max_length=120)
    id: str = ''
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=2000)
    kind: Literal['chat', 'edit'] = 'chat'
    depends_on: list[str] = Field(default_factory=list, max_length=40)
    files: list[str] = Field(default_factory=list, max_length=30)
    acceptance: list[str] = Field(min_length=1, max_length=40)
    candidates: list[str] = Field(default_factory=list, max_length=40)
    fixed_model: str = ''
    session_policy: Literal['new_session', 'reuse_session'] = 'new_session'

    @field_validator('title', 'objective')
    @classmethod
    def clean_text(cls, value: str) -> str:
        """Whitespace alone is not a plan step."""
        if not value.strip():
            raise PlanError('empty_stage', '阶段标题和目标不能为空')
        return value.strip()

    @field_validator('acceptance')
    @classmethod
    def clean_acceptance(cls, values: list[str]) -> list[str]:
        """Every generated stage must have observable acceptance evidence."""
        if any(not value.strip() or len(value) > 2000 for value in values):
            raise PlanError('acceptance', '阶段验收条目不能为空且每条最多 2000 字')
        return [value.strip() for value in values]


class PlanPatch(Boundary):
    base_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)
    stages: list[StageSpec] = Field(min_length=1, max_length=MAX_STAGES)
    authority_epoch: int | None = Field(default=None, ge=0)


class DecisionRequest(Boundary):
    question: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=4000)
    options: list[str] = Field(min_length=2, max_length=12)
    approval_option: str = Field(min_length=1, max_length=2000)
    recommendation: str = ''
    evidence_refs: list[str] = Field(default_factory=list, max_length=40)
    affected_stage_ids: list[str] = Field(default_factory=list, max_length=200)
    kind: Literal['exception', 'final'] = 'exception'
    artifact_digest: str = ''

    @field_validator('options')
    @classmethod
    def clean_options(cls, values: list[str]) -> list[str]:
        """An answer must select one explicit, unique option."""
        clean = [value.strip() for value in values]
        if any(not value or len(value) > 2000 for value in clean) or len(set(clean)) != len(clean):
            raise PlanError('options', '审核选项必须非空、互不相同且每项最多 2000 字')
        return clean

    @model_validator(mode='after')
    def explicit_approval(self) -> Self:
        """Continuing the current proposal is an explicit label, never an option position."""
        if self.approval_option not in self.options:
            raise PlanError('approval_option', '必须明确声明哪个审核选项允许继续当前提案，且标签须与选项完全一致')
        return self


class DecisionAnswer(Boundary):
    subject_hash: str = Field(min_length=1)
    authority_epoch: int = Field(ge=0)
    answer: str = Field(default='', max_length=2000)
    option_id: str = Field(default='', max_length=120)
    notes: str = Field(default='', max_length=4000)


class FinalReview(TypedDict):
    required: bool
    approved: bool
    pending_decision_id: str


class PlanView(TypedDict):
    goal_id: str
    criteria: list[str]
    review_policy: str
    allowed_files: list[str]
    file_scope: Literal['project', 'selected']
    plan_revision: int
    contract_revision: int
    history: list[dict[str, JsonValue]]
    stages: list[dict[str, JsonValue]]
    final_review: FinalReview
