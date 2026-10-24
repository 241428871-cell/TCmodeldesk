"""Bounded reads of registered native output; never attaches to stdin or a process."""

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from stat import S_ISREG
from typing import BinaryIO, Final, Literal

from pydantic import Field

from model_desk_control.trace_models import FlowRecord

TAIL_BYTES: Final = 32768
METADATA_BYTES: Final = 262144
CONTROL_BOUNDARY: Final = 32
type OutputChannel = Literal["stdout", "stderr"]


class NativeReadError(ValueError):
    """A record is missing, ambiguous, linked outside its scope or over budget."""

    def __init__(self, code: str) -> None:
        """Keep paths and captured output out of generic error messages."""
        self.code: str = code
        super().__init__(code)


class OutputTail(FlowRecord):
    """Plain text from one fixed output source, with an explicit truncation gap."""

    channel: OutputChannel
    text: str = Field(max_length=TAIL_BYTES)
    total_bytes: int
    omitted_bytes: int
    available: bool


def owned_path(project: Path, relative: Path) -> Path:
    """Reject symlinks in every record component before any file is opened."""
    root = project.resolve(strict=True)
    if relative.is_absolute() or ".." in relative.parts:
        code = "invalid_record_path"
        raise NativeReadError(code)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            code = "linked_record_path"
            raise NativeReadError(code)
    if not current.resolve().is_relative_to(root):
        code = "foreign_record_path"
        raise NativeReadError(code)
    return current


def file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """Ignore access time while detecting replacement, growth and in-place modification."""
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


@contextmanager
def regular_file(path: Path) -> Generator[BinaryIO]:
    """Nonblocking open rejects a linked or nonregular final record component."""
    if not S_ISREG(path.stat().st_mode):
        code = "non_regular_record"
        raise NativeReadError(code)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not S_ISREG(os.fstat(stream.fileno()).st_mode):
            code = "non_regular_record"
            raise NativeReadError(code)
        yield stream


def read_metadata(path: Path, *, byte_limit: int = METADATA_BYTES) -> str:
    """Read a regular bounded metadata record without changing its contents."""
    if not 0 <= byte_limit <= METADATA_BYTES:
        code = "invalid_metadata_read_budget"
        raise NativeReadError(code)
    with regular_file(path) as stream:
        before = os.fstat(stream.fileno())
        raw = stream.read(byte_limit + 1)
        if len(raw) > byte_limit:
            code = "metadata_budget_exceeded"
            raise NativeReadError(code)
        if (
            file_identity(before) != file_identity(os.fstat(stream.fileno()))
            or file_identity(before) != file_identity(path.stat())
            or len(raw) != before.st_size
        ):
            code = "metadata_changed_during_read"
            raise NativeReadError(code)
    return raw.decode("utf-8")


def output_tail(project: Path, relative: Path, channel: OutputChannel) -> OutputTail:
    """Return at most 32 KiB; missing output is unknown rather than an empty success."""
    path = owned_path(project, relative)
    try:
        with regular_file(path) as stream:
            size = stream.seek(0, 2)
            offset = max(0, size - TAIL_BYTES)
            _ = stream.seek(offset)
            raw = stream.read(TAIL_BYTES)
    except FileNotFoundError:
        return OutputTail(channel=channel, text="", total_bytes=0, omitted_bytes=0, available=False)
    # DOM text is inert. Show escape/control bytes visibly instead of executing OSC or ANSI.
    decoded = raw.decode("utf-8", errors="replace")
    text = "".join(
        char if char in "\n\t" or ord(char) >= CONTROL_BOUNDARY else "�" for char in decoded
    )
    return OutputTail(
        channel=channel, text=text, total_bytes=size, omitted_bytes=offset, available=True
    )
