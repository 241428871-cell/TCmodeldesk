# The fixture's settings-only endpoint mutation; it never dispatches a model.
from pydantic import JsonValue

def update(
    endpoint: str, body: dict[str, JsonValue], settings: dict[str, JsonValue]
) -> dict[str, JsonValue]: ...
