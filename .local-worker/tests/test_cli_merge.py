"""Native result writeback must preserve intervening human edits and its proposal."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from model_desk_control.writeback_receipt import WritebackReceipt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_worker
import delegation_policy
import execution_endpoints
import model_registry as registry
import native_write_sandbox
import workflow_settings
from test_execution_leases import fixture as fixture

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import JsonValue
    from test_execution_leases import LeaseFixture


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, str], None]:
    def gate(*_args: JsonValue, **_kwargs: JsonValue) -> None:
        return None

    monkeypatch.setattr(execution_endpoints, "gate", gate)
    monkeypatch.setattr(workflow_settings, "snapshot", lambda: {
        "paid_allowed": True, "enforce_paid_budget": False, "enforce_model_call_limit": False
    })
    # These tests exercise atomic writeback across disposable copies.  The
    # macOS Seatbelt contract has dedicated tests; an external synchronization
    # barrier here must not turn a writeback test into a sandbox test.
    monkeypatch.setattr(
        native_write_sandbox,
        "protect",
        lambda request: native_write_sandbox.ProtectedLaunch(
            argv=request.argv, mode="disposable-project-copy"
        ),
    )

    def replace_script(script: str, argument: str) -> None:
        def command(
            _engine: str, _model: str, _project: Path, _result: Path, _kind: str,
            _runtime: cli_worker.CliRuntime | None = None,
        ) -> list[str]:
            return [sys.executable, "-c", script, argument]

        monkeypatch.setattr(cli_worker, "command", command)

    return replace_script


def test_conflict_in_last_file_prevents_all_writeback_and_retains_proposal(
    fixture: LeaseFixture, native: Callable[[str, str], None]
) -> None:
    project = fixture.project
    for name in ("a.py", "b.py"):
        _ = (project / name).write_text("base")
    run = fixture.task("conflict", ("a.py", "b.py"))
    _ = (run / "prompt.txt").write_text("fixture")
    native(
        dedent("""
            import sys
            from pathlib import Path
            sys.stdin.read()
            Path('a.py').write_text('proposal-a')
            Path('b.py').write_text('proposal-b')
            Path(sys.argv[1]).write_text('human')
            print('done')
        """),
        str(project / "b.py"),
    )
    with pytest.raises(ValueError, match="writeback_conflict"):
        cli_worker.run_task(run, {
            "engine": "codex_cli", "kind": "edit", "project": str(project),
            "files": ["a.py", "b.py"], "timeout": 30,
        }, {"provider": "codex_cli", "model": "fixture"})
    assert (project / "a.py").read_text() == "base"
    assert (project / "b.py").read_text() == "human"
    candidates = list((run / "native-cli").glob("*/proposed-files/a.py"))
    assert len(candidates) == 1
    assert candidates[0].read_text() == "proposal-a"
    assert registry.read_task(run)["writeback_state"] == "conflict"


def test_two_native_workers_keep_both_disjoint_results(
    fixture: LeaseFixture, native: Callable[[str, str], None]
) -> None:
    project = fixture.project
    barrier = project.parent / "barrier"
    barrier.mkdir()
    for name in ("a.py", "b.py"):
        _ = (project / name).write_text("base")
    native(dedent("""
        import sys,time,json
        from pathlib import Path
        prompt=sys.stdin.read()
        name='a.py' if prompt.startswith('a.py') else 'b.py'
        barrier=Path(sys.argv[1])
        (barrier/name).write_text('ready')
        deadline=time.monotonic()+3
        while len(list(barrier.iterdir())) < 2 and time.monotonic() < deadline: time.sleep(.01)
        assert len(list(barrier.iterdir())) == 2
        Path(name).write_text('result-'+name)
        print(json.dumps({'type':'result','subtype':'success','is_error':False,
                         'session_id':'fixture-session','result':'done'}))
    """), str(barrier))
    failures: list[ValueError | OSError] = []

    def execute(engine: str, name: str) -> None:
        run = fixture.task(engine, (name,))
        _ = (run / "prompt.txt").write_text(name)
        try:
            cli_worker.run_task(run, {
                "engine": engine, "kind": "edit", "project": str(project),
                "files": [name], "timeout": 30,
            }, {"provider": engine, "model": "fixture"})
        except (ValueError, OSError) as error:
            failures.append(error)

    workers = [threading.Thread(target=execute, args=(engine, name)) for engine, name in
               (("codex_cli", "a.py"), ("claude_cli", "b.py"))]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(8)
        assert not worker.is_alive()
    assert failures == []
    assert (project / "a.py").read_text() == "result-a.py"
    assert (project / "b.py").read_text() == "result-b.py"


def test_failed_test_retains_candidate_without_authorizing_recovery(
    fixture: LeaseFixture, native: Callable[[str, str], None]
) -> None:
    project = fixture.project
    run = fixture.task("failed-test")
    _ = (run / "prompt.txt").write_text("fixture")
    script = "import sys;from pathlib import Path;sys.stdin.read()"
    script += ";Path('a.py').write_text('candidate')"
    native(script, "")
    with pytest.raises(cli_worker.CliRuntimeError, match="测试失败"):
        cli_worker.run_task(run, {
            "engine": "codex_cli", "kind": "edit", "project": str(project),
            "files": ["a.py"], "timeout": 30, "test_command": "exit 7",
        }, {"provider": "codex_cli", "model": "fixture"})
    assert not (project / "a.py").exists()
    capture = next((run / "native-cli").iterdir())
    assert (capture / "proposed-files/a.py").read_text() == "candidate"
    content = (capture / "writeback-receipt.json").read_bytes()
    receipt = WritebackReceipt.model_validate_json(content)
    assert not receipt.eligible
    assert receipt.test is not None
    assert receipt.test.exit_code == 7
    assert registry.read_task(run)["writeback_state"] == "test_failed"


def test_successful_delegated_cli_result_waits_for_independent_acceptance(
    fixture: LeaseFixture, native: Callable[[str, str], None]
) -> None:
    project = fixture.project
    _ = (project / "a.py").write_text("base")
    run = fixture.task("delegated-cli")
    _ = registry.patch_task(
        run,
        route={"requirements": {"acceptance": ["a.py contains the executor result"]}},
        delegation={"schema": 1, "rework_count": 0},
    )
    _ = (run / "prompt.txt").write_text("update a.py")
    native(
        "import sys;from pathlib import Path;sys.stdin.read();"
        "Path('a.py').write_text('executor-result');print('done')",
        "",
    )

    cli_worker.run_task(
        run,
        {
            **registry.read_task(run),
            "engine": "codex_cli",
            "kind": "edit",
            "project": str(project),
            "files": ["a.py"],
            "timeout": 30,
        },
        {"provider": "codex_cli", "model": "fixture"},
    )

    task = registry.read_task(run)
    review = delegation_policy.load(run)
    assert task["status"] == "completed"
    assert task["review_state"] == delegation_policy.EXECUTOR_COMPLETED
    assert review is not None
    assert review["state"] == delegation_policy.EXECUTOR_COMPLETED
    assert review["executor"]["status"] == "completed"
    assert review["executor"]["changed_files"] == ["a.py"]
    assert (project / "a.py").read_text() == "executor-result"
