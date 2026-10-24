"""Human reconciliation retains project state without granting quality acceptance."""

from dataclasses import dataclass
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from model_desk_control.host_flow import HostRecord, HostRef
from model_desk_control.host_trace_models import RecordId

type Record = dict[str, JsonValue]
type ProcessState = Literal["running", "exited", "unknown", "not_started", "not_applicable"]


class ResolutionError(ValueError):
    """A stale, incomplete or unrelated observation cannot retire an original round."""

    def __init__(self, code: str) -> None:
        """Expose a stable reason without returning captured model output."""
        self.code: str = code
        super().__init__(code)


class ResolutionInput(HostRecord):
    """The browser selects an observed original round, never supplies its outcome."""

    intent_id: RecordId
    authority_epoch: int = Field(ge=0)
    subject_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    action: Literal["retain_state"]
    notes: Annotated[str, Field(min_length=1, max_length=4000)]
    reviewed_workspace: Literal[True]

    @field_validator("notes")
    @classmethod
    def meaningful_notes(cls, value: str) -> str:
        """A checkbox alone is not a record of the human's actual inspection."""
        clean = value.strip()
        if not clean:
            code = "resolution_notes_required"
            raise ResolutionError(code)
        return clean


class ResolutionClock(BaseModel):
    """Current control is preserved even when the human retires an earlier epoch."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    project: str
    goal_id: str
    authority_epoch: int
    active_intent_id: str
    user_intent: str
    enabled: bool
    closed: bool
    hold: Record


class ResolutionIntent(BaseModel):
    """The stored identity must agree with its SQL owner and original graph scope."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    id: RecordId
    goal_id: RecordId
    authority_epoch: int
    owner_id: str
    status: str
    pid: int | None = None
    birth: float | None = None
    resolution: Record | None = None
    host_protocol: Literal["legacy", "graph_v2"] = "legacy"


class PhaseFact(HostRecord):
    """Only hashes and phase status leave the inventory, never full native context."""

    step: str
    status: str
    version: str
    scope_hash: str
    result_digest: str


class RequestFact(HostRecord):
    """Original request references remain visible after human reconciliation."""

    request_id: str
    kind: str
    state: str
    task_id: str
    payload_digest: str
    receipt_digest: str


class TaskFact(HostRecord):
    """A terminal label does not by itself prove a native child has exited."""

    task_id: RecordId
    status: str
    engine: str
    process_state: ProcessState
    metadata_digest: str
    blocker: str = ""
    title: str = ""


class ResolutionFacts(HostRecord):
    """The exact evidence to which a human's decision is bound."""

    scope: HostRef
    authority_epoch: int
    goal_status: str
    user_intent: str
    enabled: bool
    clock_digest: str
    intent_digest: str
    stage_digest: str
    plan_revision: int
    contract_revision: int
    phases: tuple[PhaseFact, ...]
    requests: tuple[RequestFact, ...]
    tasks: tuple[TaskFact, ...]
    process_state: ProcessState


class ResolutionView(HostRecord):
    """An on-demand read never launches a model or changes the original clock."""

    facts: ResolutionFacts
    subject_hash: str
    can_resolve: bool
    blockers: tuple[str, ...]
    declared_files: tuple[str, ...]
    declared_files_omitted: int = 0
    workspace_note: str = (
        "此清单只列阶段声明文件，不能证明主脑没有直接修改其他文件。"
        "请核对工作目录；保留现状不代表成果通过验收。"
    )


class ResolutionReceipt(HostRecord):
    """An immutable human lifecycle decision, distinct from task acceptance."""

    actor: Literal["human"] = "human"
    action: Literal["retain_state"] = "retain_state"
    scope: HostRef
    intent_id: str
    original_owner_id: str
    subject_hash: str
    input_digest: str
    notes: str
    at: str
    authority_epoch: int
    resulting_epoch: int
    quality_accepted: Literal[False] = False


@dataclass(frozen=True, slots=True)
class ResolutionRecords:
    """Raw JSON is retained solely for atomic updates that preserve unrelated fields."""

    clock: Record
    intent: Record
    parsed_clock: ResolutionClock
    parsed_intent: ResolutionIntent
    goal_status: str


@dataclass(frozen=True, slots=True)
class ResolutionCommit:
    """Replayed decisions return their original receipt without changing current control."""

    receipt: ResolutionReceipt
    replayed: bool


class ResolutionResult(HostRecord):
    """Return current control separately from the immutable historical decision."""

    receipt: ResolutionReceipt
    replayed: bool
    clock: Record
