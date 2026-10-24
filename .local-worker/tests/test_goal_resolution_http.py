"""Actual loopback human routes preserve project scope, tokens and original decisions."""

from __future__ import annotations

import json
import sys
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from pydantic import JsonValue, TypeAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import continuation_clock
import goal_brain_runner
import panel_server as panel
from test_goal_action_receipts import run as run
from test_goal_resolution import cancelled_work, clock
from test_goal_resume import saved_actions

if TYPE_CHECKING:
    from collections.abc import Generator

    from test_goal_action_receipts import GoalRun

type Record = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    body: Record


@dataclass(frozen=True, slots=True)
class Browser:
    address: str
    project: str

    def request(
        self,
        path: str,
        body: Record | None = None,
        *,
        authorized: bool = True,
        project: str = "",
    ) -> Reply:
        view = {"mode": "project", "project": project or self.project}
        encoded = urlsafe_b64encode(json.dumps(view).encode()).decode()
        headers = {"X-Model-Desk-View": encoded, "Content-Type": "application/json"}
        if authorized:
            headers["X-Panel-Token"] = panel.TOKEN
        connection = HTTPConnection(self.address, timeout=3)
        try:
            connection.request(
                "GET" if body is None else "POST",
                path,
                body=None if body is None else json.dumps(body).encode(),
                headers=headers,
            )
            response = connection.getresponse()
            return Reply(response.status, TypeAdapter(Record).validate_json(response.read()))
        finally:
            connection.close()


@pytest.fixture
def browser(run: GoalRun) -> Generator[Browser]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
    server.daemon_threads = True
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield Browser(f"127.0.0.1:{server.server_port}", run.scope.project)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def inspected(browser: Browser, run: GoalRun) -> Record:
    reply = browser.request(f"/api/governance/goals/{run.scope.goal_id}/recovery")
    assert reply.status == 200, reply.body
    return reply.body


def decision(view: Record, run: GoalRun) -> Record:
    return {
        "intent_id": run.scope.operation_id,
        "authority_epoch": clock(run)["authority_epoch"],
        "subject_hash": view["subject_hash"],
        "action": "retain_state",
        "reviewed_workspace": True,
        "notes": "Human checked the fixture workspace and original receipts.",
    }


def test_inspection_is_read_only_and_uses_actual_process_inventory(
    browser: Browser, run: GoalRun
) -> None:
    saved_actions(run)
    before = clock(run)
    with patch.object(goal_brain_runner, "run") as native:
        result = inspected(browser, run)
    native.assert_not_called()
    assert result["can_resolve"] is False
    assert result["blockers"]
    assert clock(run) == before
    assert len(run.queued) == 1


def test_reconciliation_requires_existing_panel_token(browser: Browser, run: GoalRun) -> None:
    cancelled_work(run)
    payload = decision(inspected(browser, run), run)
    before = clock(run)
    reply = browser.request(
        f"/api/governance/goals/{run.scope.goal_id}/reconcile", payload, authorized=False
    )
    assert reply.status == 403
    assert reply.body["code"] == "panel_session_expired"
    assert clock(run) == before


def test_inspection_and_decision_cannot_change_view_project(
    browser: Browser, run: GoalRun, tmp_path: Path
) -> None:
    cancelled_work(run)
    payload = decision(inspected(browser, run), run)
    other = tmp_path / "other-project"
    other.mkdir()
    before = clock(run)
    get = browser.request(
        f"/api/governance/goals/{run.scope.goal_id}/recovery", project=str(other)
    )
    post = browser.request(
        f"/api/governance/goals/{run.scope.goal_id}/reconcile",
        {**payload, "project": str(other)},
    )
    assert get.status == 400
    assert "subject_hash" not in get.body
    assert post.status == 403
    assert clock(run) == before


def test_browser_replay_returns_original_receipt_and_keeps_later_stop(
    browser: Browser, run: GoalRun
) -> None:
    cancelled_work(run)
    view = inspected(browser, run)
    assert view["can_resolve"] is True
    payload = decision(view, run)
    path = f"/api/governance/goals/{run.scope.goal_id}/reconcile"
    with patch.object(goal_brain_runner, "run") as native:
        first = browser.request(path, payload)
        _ = continuation_clock.control(run.scope.project, run.scope.goal_id, "stop")
        stopped = clock(run)
        replay = browser.request(path, payload)
    native.assert_not_called()
    assert first.status == replay.status == 200, (first.body, replay.body)
    assert first.body["receipt"] == replay.body["receipt"]
    assert replay.body["replayed"] is True
    assert clock(run) == stopped
    assert len(run.queued) == 1
