from typing import Literal

def inspect_owned(
    pid: int, birth: float
) -> tuple[Literal["running", "exited", "unknown"], str]: ...
