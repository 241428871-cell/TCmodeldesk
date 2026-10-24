"""Publish shared adapter declarations without inspecting accounts or starting workers."""

import control_bootstrap
from pydantic import JsonValue, TypeAdapter


def describe(engine: str, role: str) -> dict[str, JsonValue]:
    """This is configured capability metadata, never a runtime acceptance receipt."""
    control_bootstrap.bind_source()
    from model_desk_control.enforcement import report

    return TypeAdapter(dict[str, JsonValue]).validate_json(report(engine, role).model_dump_json())
