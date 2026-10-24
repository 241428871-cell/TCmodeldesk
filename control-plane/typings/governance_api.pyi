from goal_actions import Panel
from pydantic import JsonValue

type Record = dict[str, JsonValue]

def get(path: str, view: Record) -> Record | None: ...
def post(path: str, body: Record, view: Record, panel: Panel) -> Record | None: ...
