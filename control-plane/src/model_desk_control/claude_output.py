"""Bounded Claude message-block streams; only a verified terminal result is a delivery."""

from typing import ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

STREAM_ARGUMENTS: Final = (
    "--output-format",
    "stream-json",
    "--verbose",
    "--forward-subagent-text",
)
MAX_STREAM_BYTES: Final = 4 * 1024 * 1024
_EVENT: Final = TypeAdapter(dict[str, JsonValue])


class ClaudeOutputError(ValueError):
    """A stable protocol reason that does not expose native output or account data."""

    def __init__(self, code: str) -> None:
        """Preserve the exact reason for the host's existing failure boundary."""
        self.code: str = code
        super().__init__(code)


class _Envelope(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    type: str = Field(min_length=1)


class ClaudeCompletion(BaseModel):
    """The main session's explicit terminal result, independent of intermediate blocks."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    type: Literal["result"]
    subtype: str
    is_error: bool
    session_id: str = Field(min_length=1, max_length=256)
    parent_tool_use_id: str | None = None
    result: str = ""
    structured_output: dict[str, JsonValue] | None = None


def completion_from_jsonl(raw: str, *, expected_session: str | None = None) -> ClaudeCompletion:
    """Reject truncation, multiple results, child results and identity mismatches."""
    if len(raw.encode("utf-8")) > MAX_STREAM_BYTES:
        code = "claude_stream_size_exceeded"
        raise ClaudeOutputError(code)
    terminal: ClaudeCompletion | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if terminal is not None:
            code = "claude_stream_continues_after_result"
            raise ClaudeOutputError(code)
        try:
            event = _EVENT.validate_json(line, strict=True)
            envelope = _Envelope.model_validate(event)
            if envelope.type != "result":
                continue
            terminal = ClaudeCompletion.model_validate(event)
        except ValidationError as error:
            code = "claude_stream_invalid"
            raise ClaudeOutputError(code) from error
        if (
            terminal.is_error
            or terminal.subtype != "success"
            or terminal.parent_tool_use_id is not None
        ):
            code = "claude_result_failed"
            raise ClaudeOutputError(code)
        if expected_session is not None and terminal.session_id != expected_session:
            code = "claude_session_mismatch"
            raise ClaudeOutputError(code)
    if terminal is None:
        code = "claude_result_missing"
        raise ClaudeOutputError(code)
    return terminal


class ClaudeSession(BaseModel):
    """Effective settings the CLI reports for the main session, not the requested ones."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, strict=True, extra="ignore")
    model: str = Field(default="", max_length=200)
    permission_mode: str = Field(default="", max_length=50)
    version: str = Field(default="", max_length=50)


class _Init(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        frozen=True, strict=True, extra="ignore", populate_by_name=True
    )
    type: Literal["system"]
    subtype: Literal["init"]
    parent_tool_use_id: str | None = None
    model: str = ""
    permission_mode: str = Field(default="", alias="permissionMode")
    version: str = Field(default="", alias="claude_code_version")


def session_from_jsonl(raw: str) -> ClaudeSession | None:
    """Read the main session's init event; subagent or malformed lines are ignored."""
    for line in raw.splitlines()[:200]:
        try:
            init = _Init.model_validate(_EVENT.validate_json(line, strict=True))
        except ValidationError:
            continue
        if init.parent_tool_use_id is None:
            return ClaudeSession(
                model=init.model[:200],
                permission_mode=init.permission_mode[:50],
                version=init.version[:50],
            )
    return None
