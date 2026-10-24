"""Exact UI proxy additions; native output is the only route with a bounded query."""

import re
from typing import Final
from urllib.parse import parse_qsl, urlsplit

GET: Final = frozenset(
    {"/api/workspace", "/api/governance", "/api/engineering-policy", "/api/flow", "/api/connection-accounts"}
)
POST: Final = frozenset(
    {
        "/api/workspace/projects",
        "/api/workspace/bindings",
        "/api/workspace/defaults",
        "/api/provider-catalog",
        "/api/accounts/refresh",
        "/api/connection-accounts",
        "/api/connection-accounts/delete",
        "/api/connection-accounts/balance",
        "/api/collaboration-policy",
        "/api/engineering-policy",
        "/api/governance/goals",
    }
)
IDENTIFIER: Final = r"[A-Za-z0-9_-]{1,100}"
RECOVERY: Final = re.compile(rf"/api/governance/goals/{IDENTIFIER}/recovery")
PROGRESS: Final = re.compile(rf"/api/governance/goals/{IDENTIFIER}/progress")
RESOURCES: Final = re.compile(rf"/api/tasks/{IDENTIFIER}/resources")
GOAL_WRITE: Final = re.compile(
    rf"/api/governance/goals/{IDENTIFIER}/(contract|control|reconcile|interactions)"
)
ANSWER: Final = re.compile(rf"/api/governance/decisions/{IDENTIFIER}/answer")
CLI_SETTINGS: Final = re.compile(r"/api/execution-endpoints/(claude_cli|codex_cli)")
CLI_UPDATE: Final = re.compile(r"/api/cli-updates/(dsh|claude_cli|codex_cli)/(check|update)")
SESSION: Final = re.compile(rf"/api/flow/sessions/(brain|task)(?:%3[Aa]|:){IDENTIFIER}")
CAPTURE: Final = re.compile(r"[A-Za-z0-9_-]{0,100}")
MAX_PATH: Final = 1024


def _output(path: str) -> bool:
    if not path.startswith("/api/flow/sessions/"):
        return False
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or not SESSION.fullmatch(parsed.path):
        return False
    try:
        pairs = parse_qsl(
            parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3
        )
    except ValueError:
        return False
    if len({key for key, _ in pairs}) != len(pairs):
        return False
    return all(
        (key == "channel" and value in {"stdout", "stderr"})
        or (key == "capture_id" and bool(CAPTURE.fullmatch(value)))
        or (key == "view" and value in {"raw", "events"})
        for key, value in pairs
    )


def accepts(path: str, method: str) -> bool:
    """Keep account, governance and output access on the existing UI-only transport."""
    if len(path) > MAX_PATH or any(character in path for character in "#\\ \t\n\r"):
        return False
    if method == "GET":
        return (
            path in GET or bool(
                RECOVERY.fullmatch(path) or PROGRESS.fullmatch(path) or RESOURCES.fullmatch(path)
            )
            or _output(path)
        )
    if method == "POST":
        return (
            path in POST
            or bool(GOAL_WRITE.fullmatch(path))
            or bool(ANSWER.fullmatch(path))
            or bool(CLI_SETTINGS.fullmatch(path))
            or bool(CLI_UPDATE.fullmatch(path))
        )
    return False
