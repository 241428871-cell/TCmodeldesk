"""Project autonomy preserves selected-file authority and rejects control-record aliases."""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from model_desk_control.file_authority import FileAuthority, FileAuthorityError, choose_file_scope


def test_new_goal_default_and_criteria_only_changes_have_distinct_authority() -> None:
    assert choose_file_scope(None, supplied_files=False, previous=None) == "project"
    assert choose_file_scope(None, supplied_files=True, previous=None) == "selected"
    assert choose_file_scope(None, supplied_files=False, previous="selected") == "selected"
    assert choose_file_scope(None, supplied_files=False, previous="project") == "project"
    assert choose_file_scope("project", supplied_files=True, previous="selected") == "project"


def test_ai_can_select_new_project_files_without_creating_them(tmp_path: Path) -> None:
    files = FileAuthority(scope="project").validate_paths(tmp_path, ("src\\new.py", "README.md"))
    assert files == ("src/new.py", "README.md")
    assert list(tmp_path.iterdir()) == []


def test_missing_historical_scope_is_selected_and_cannot_grant_an_edit(tmp_path: Path) -> None:
    with pytest.raises(FileAuthorityError, match="file_not_selected"):
        _ = FileAuthority().validate_paths(tmp_path, ("new.py",))
    selected = FileAuthority(allowed_files=("src\\old.py",))
    assert selected.validate_paths(tmp_path, ("src/old.py",)) == ("src/old.py",)
    with pytest.raises(FileAuthorityError, match="file_not_selected"):
        _ = selected.validate_paths(tmp_path, ("src/new.py",))


@pytest.mark.parametrize(
    "name",
    [
        "../outside.py",
        "/absolute.py",
        "C:\\private.py",
        "*.py",
        "src",
        "bad\nfile.py",
    ],
)
def test_invalid_or_directory_targets_never_become_file_authority(
    tmp_path: Path, name: str
) -> None:
    (tmp_path / "src").mkdir()
    with pytest.raises(FileAuthorityError):
        _ = FileAuthority(scope="project").validate_paths(tmp_path, (name,))


@pytest.mark.parametrize(
    "name",
    [
        ".model-desk/records.db",
        "nested/.MODEL-DESK/task.json",
        ".git/config",
        ".codex/auth.json",
        ".aws/credentials",
        ".claude/settings.json",
        ".local-worker/models.json",
        ".local-worker/budget-ledger.json",
        ".local-worker/endpoint-access.json",
        ".local-worker/backups/account.json",
        ".local-worker/agent-runtime/state/current.json",
    ],
)
def test_project_and_selected_modes_both_protect_runtime_records(tmp_path: Path, name: str) -> None:
    for authority in (FileAuthority(scope="project"), FileAuthority(allowed_files=(name,))):
        with pytest.raises(FileAuthorityError, match="protected_control_path"):
            _ = authority.validate_paths(tmp_path, (name,))


@pytest.mark.skipif(os.name == "nt", reason="Symlinks require Windows developer-mode privileges")
def test_current_symlink_targets_are_rechecked_before_every_dispatch(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    records = project / ".model-desk"
    records.mkdir()
    _ = (records / "private.json").write_text("private fixture")
    alias = project / "alias"
    alias.symlink_to(records, target_is_directory=True)
    with pytest.raises(FileAuthorityError, match="protected_control_path"):
        _ = FileAuthority(scope="project").validate_paths(project, ("alias/private.json",))
    alias.unlink()
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(FileAuthorityError, match="file_outside_project"):
        _ = FileAuthority(scope="project").validate_paths(project, ("alias/outside.py",))


def test_application_sources_are_editable_without_authorizing_installation_records(
    tmp_path: Path,
) -> None:
    project = tmp_path / ".omo/worktrees/app"
    project.mkdir(parents=True)
    authority = FileAuthority(scope="project")
    assert authority.validate_paths(project, (".local-worker/goal_controller.py",)) == (
        ".local-worker/goal_controller.py",
    )
    runtime = project / ".local-worker"
    runtime.mkdir()
    with pytest.raises(FileAuthorityError, match="protected_control_path"):
        _ = authority.validate_paths(runtime, ("models.json",))


@pytest.mark.parametrize("directory", [".model-desk", ".git", ".codex", ".claude", ".aws"])
def test_selecting_record_directory_as_project_does_not_make_its_contents_writable(
    tmp_path: Path,
    directory: str,
) -> None:
    project = tmp_path / directory / "nested"
    project.mkdir(parents=True)
    with pytest.raises(FileAuthorityError, match="protected_control_path"):
        _ = FileAuthority(scope="project").validate_paths(project, ("task.json",))


def test_unknown_scope_is_rejected_instead_of_becoming_project_permission() -> None:
    with pytest.raises(ValidationError):
        _ = FileAuthority.model_validate_json('{"scope":"all"}')
