"""Native decision models work in stable copies; only structured actions leave them."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import goal_brain_runner
import goal_brain_workspace
import model_registry as registry


@pytest.fixture
def installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "installation"
    root.mkdir()
    monkeypatch.setattr(registry, "HERE", root)
    return root


def test_workspace_is_stable_refreshed_and_excludes_control_records(
    tmp_path: Path, installation: Path
) -> None:
    # Given project sources, private records and a link outside the project.
    project = tmp_path / "project"
    project.mkdir()
    _ = (project / "source.py").write_text("original", encoding="utf-8")
    (project / ".model-desk").mkdir()
    _ = (project / ".model-desk" / "private.json").write_text("secret", encoding="utf-8")
    (project / ".local-worker").mkdir()
    _ = (project / ".local-worker" / "settings.json").write_text("secret", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    _ = outside.write_text("outside", encoding="utf-8")
    (project / "alias.txt").symlink_to(outside)

    # When the Goal's native workspace is prepared and modified by an auto brain.
    first = goal_brain_workspace.prepare(str(project.resolve()), "goal_fixture")
    _ = (first / "source.py").write_text("direct brain edit", encoding="utf-8")

    # Then the original is untouched and no records, runtime home or links were copied.
    assert (project / "source.py").read_text(encoding="utf-8") == "original"
    assert not (first / ".model-desk").exists()
    assert not (first / ".local-worker").exists()
    assert not (first / "alias.txt").exists()

    # And the next round keeps the session cwd but refreshes it from authoritative sources.
    second = goal_brain_workspace.prepare(str(project.resolve()), "goal_fixture")
    assert second == first
    assert (second / "source.py").read_text(encoding="utf-8") == "original"
    assert not tuple(second.parent.glob(".native-workspace-*.tmp"))
    assert not tuple(second.parent.glob(".native-workspace-*.old"))


def test_native_context_rebinds_project_and_removes_original_record_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "copy"
    context = {
        "project": str(tmp_path / "original"),
        "goal_id": "goal_fixture",
        "engineering_policy": {"snapshot": {"project": str(tmp_path / "original")}},
        "tasks": [
            {
                "id": "task_1",
                "record_directory": str(tmp_path / "original" / ".model-desk" / "runs"),
                "answer": "bounded result",
            }
        ],
    }

    rebound = goal_brain_workspace.native_context(context, workspace)

    assert rebound["project"] == str(workspace)
    assert rebound["tasks"] == [{"id": "task_1", "answer": "bounded result"}]
    assert rebound["engineering_policy"] == {"snapshot": {"project": str(workspace)}}
    assert context["tasks"][0]["record_directory"].endswith(".model-desk/runs")


def test_native_runner_executes_auto_brain_in_copy_and_only_returns_actions(
    tmp_path: Path, installation: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an ordinary project and a harmless native-process fixture.
    project = tmp_path / "project"
    project.mkdir()
    _ = (project / "source.py").write_text("original", encoding="utf-8")
    script = tmp_path / "native_fixture.py"
    _ = script.write_text(
        """import json, pathlib, sys
prompt = sys.stdin.read()
pathlib.Path('received-prompt.txt').write_text(prompt)
pathlib.Path('direct-edit.txt').write_text('must stay disposable')
actions = {'plan': None, 'questions': [], 'dispatch_stage_ids': [], 'reviews': [],
           'obligations': [], 'complete': False, 'summary': 'fixture'}
pathlib.Path(sys.argv[1]).write_text(json.dumps(actions))
print(json.dumps({'type': 'thread.started', 'thread_id': 'fixture-session'}), flush=True)
""",
        encoding="utf-8",
    )

    requested_sessions: list[str] = []

    def command(
        _engine: str,
        _model: str,
        workspace: Path,
        output: Path,
        _schema: Path,
        _runtime: object,
        requested_session: str,
        _permission_mode: str,
    ) -> tuple[list[str], str]:
        assert workspace.name == "native-workspace"
        requested_sessions.append(requested_session)
        return [sys.executable, str(script), str(output)], "fixture-session"

    monkeypatch.setattr(goal_brain_runner, "command", command)
    context = {
        "project": str(project.resolve()),
        "goal_id": "goal_fixture",
        "tasks": [{"id": "task_1", "record_directory": str(project / ".model-desk/runs")}],
    }
    directory = project / ".model-desk" / "goal-brain" / "goal_fixture" / "round_fixture"

    # When the real subprocess runner executes an auto round.
    result = goal_brain_runner.run(
        context,
        "codex_cli",
        "fixture-model",
        {},
        directory,
        lambda *_: None,
        lambda: False,
        session_id="legacy-session",
    )

    # Then direct native writes remain disposable and only validated actions return.
    workspace = directory.parent / "native-workspace"
    assert result["actions"]["summary"] == "fixture"
    assert not (project / "direct-edit.txt").exists()
    assert (project / "source.py").read_text(encoding="utf-8") == "original"
    assert (workspace / "direct-edit.txt").read_text(encoding="utf-8") == "must stay disposable"
    prompt = (workspace / "received-prompt.txt").read_text(encoding="utf-8")
    assert '"project": "' + str(workspace) + '"' in prompt
    assert "record_directory" not in prompt
    assert (directory / "workspace.json").is_file()
    assert goal_brain_workspace.session_is_bound(
        str(project.resolve()), "goal_fixture", "fixture-session"
    )
    assert not goal_brain_workspace.session_is_bound(
        str(project.resolve()), "goal_fixture", "legacy-session"
    )

    # And a later round resumes only the session proven to use this stable copy.
    second = goal_brain_runner.run(
        context,
        "codex_cli",
        "fixture-model",
        {},
        directory.parent / "round_2",
        lambda *_: None,
        lambda: False,
        session_id="fixture-session",
    )
    assert second["native_session_id"] == "fixture-session"
    assert requested_sessions == ["", "fixture-session"]
