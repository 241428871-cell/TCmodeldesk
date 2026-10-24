"""Read-only native-host observations, distinct from control authorization."""

from dataclasses import dataclass
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from model_desk_control.host_flow import HostProfile
from model_desk_control.trace_models import FlowRecord, FlowSnapshot

RecordId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
CaptureId = Annotated[str, Field(pattern=r"^(?:[A-Za-z0-9_-]{1,100})?$")]


class LegacyObservation(BaseModel):
    """Parse only required facts from an existing legacy record."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")


class IntentObservation(LegacyObservation):
    """Native process facts come from the original continuation intent."""

    id: RecordId
    goal_id: RecordId
    status: str
    brain_model_id: str = ""
    native_session_id: str = ""
    brain_permission_mode: str = "read_only"
    pid: int | None = None
    birth: float | None = None


class TaskObservation(LegacyObservation):
    """A task association cannot override its project-owned content record."""

    id: RecordId
    project: str
    goal_id: RecordId | None = None
    stage_id: str | None = None
    authority_epoch: int | None = None
    goal_round_id: str | None = None
    goal_dispatch_request_id: str | None = None
    goal_action_payload_digest: str | None = None
    status: str
    engine: str = ""
    model_id: str = ""
    model: str = ""
    native_session_id: str = ""
    process_pid: int | None = None
    process_birth: float | None = None
    native_output_capture: int = 0
    native_capture_id: CaptureId = ""
    sandbox_mode: str = "legacy_task"
    cli_permission_mode: str = ""
    cli_runtime: HostProfile | None = None


class NativeReference(FlowRecord):
    """An output source is resolved again by the server when the inspector opens."""

    session_key: str
    record_id: RecordId
    goal_id: RecordId | Literal[""]
    role: Literal["decision", "execution"]
    model_id: str
    engine: str
    native_session_id: str
    permission_mode: str
    operation_status: str = ""
    native_capture_id: CaptureId = ""
    runtime: HostProfile | None = None


class HostTrace(FlowRecord):
    """Bounded metadata only; output text is fetched exclusively on inspection."""

    flow: FlowSnapshot
    references: tuple[NativeReference, ...]
    observation_gaps: int
    window_limited: bool
    source: Literal["domain_receipts"] = "domain_receipts"


@dataclass(frozen=True, slots=True)
class RoundMessages:
    """Keep the original journal sequence separate from message-content parsing."""

    sequence: int
    actions: str
    context: str
    native_done: bool
