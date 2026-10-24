"""External CLI selection is shared by launch, metadata, quota and updates."""
from pathlib import Path
from unittest.mock import patch
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_paths
import cli_worker
import cli_updates
import codex_usage
import model_registry as registry
import execution_endpoints


@pytest.fixture(autouse=True)
def isolated_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, 'HERE', tmp_path)
    monkeypatch.setenv('MODEL_DESK_CODEX_DESKTOP', str(tmp_path / 'Absent.app/Contents/MacOS/codex'))


def independent():
    execution_endpoints.update('codex_cli', {'cli_source': 'standalone'}, {}, idle_guard=lambda: None)


def command(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o755)
    return path


def test_app_injected_path_does_not_shadow_independent_cli(tmp_path, monkeypatch):
    bundled = command(tmp_path / 'ChatGPT.app/Contents/MacOS/codex')
    native = command(tmp_path / '.local/bin/codex')
    monkeypatch.delenv(cli_paths.CODEX_COMMAND_ENV, raising=False)
    with patch.object(cli_paths.Path, 'home', return_value=tmp_path), \
         patch.object(cli_paths.shutil, 'which', return_value=str(bundled)):
        independent()
        assert cli_paths.codex_command() == native
        assert cli_worker.availability('codex_cli')['executable'] == str(native)
        assert codex_usage.find_codex() == str(native)
        assert cli_updates._executable('codex_cli') == native


def test_only_app_binary_is_unavailable_even_through_a_user_symlink(tmp_path, monkeypatch):
    bundled = command(tmp_path / 'ChatGPT.app/Contents/MacOS/codex')
    link = tmp_path / '.local/bin/codex'
    link.parent.mkdir(parents=True)
    link.symlink_to(bundled)
    monkeypatch.delenv(cli_paths.CODEX_COMMAND_ENV, raising=False)
    monkeypatch.setenv('PATH', str(bundled.parent))
    with patch.object(cli_paths.Path, 'home', return_value=tmp_path):
        assert cli_paths.standalone_codex() is None
        assert cli_worker.availability('codex_cli')['ready'] is False


def test_explicit_path_wins_and_invalid_override_does_not_fall_back(tmp_path, monkeypatch):
    native = command(tmp_path / 'custom/codex')
    monkeypatch.setenv(cli_paths.CODEX_COMMAND_ENV, str(native))
    independent()
    assert cli_paths.codex_command() == native
    monkeypatch.setenv(cli_paths.CODEX_COMMAND_ENV, 'relative/codex')
    assert cli_paths.codex_command() is None


def test_desktop_is_default_and_ignores_external_override(tmp_path, monkeypatch):
    bundled = command(tmp_path / 'Codex.app/Contents/MacOS/codex')
    native = command(tmp_path / 'native/codex')
    monkeypatch.setenv('MODEL_DESK_CODEX_DESKTOP', str(bundled))
    monkeypatch.setenv(cli_paths.CODEX_COMMAND_ENV, str(native))
    assert cli_paths.codex_command() == bundled
    assert codex_usage.find_codex() == str(bundled)
    independent()
    assert cli_paths.codex_command() == native
    execution_endpoints.update('codex_cli', {'cli_source': 'desktop'}, {}, idle_guard=lambda: None)
    assert cli_paths.codex_command() == bundled


def test_missing_desktop_does_not_silently_use_independent(tmp_path, monkeypatch):
    native = command(tmp_path / 'native/codex')
    monkeypatch.setenv(cli_paths.CODEX_COMMAND_ENV, str(native))
    assert cli_paths.codex_command() is None


def test_claude_follows_the_current_cswap_managed_path_without_pinning_target(tmp_path):
    first = command(tmp_path / 'versions/first')
    second = command(tmp_path / 'versions/second')
    link = tmp_path / 'claude'
    link.symlink_to(first)
    with patch.object(cli_paths.shutil, 'which', return_value=str(link)):
        assert cli_paths.executable('claude_cli') == link
        link.unlink()
        link.symlink_to(second)
        assert cli_paths.executable('claude_cli') == link
        assert cli_paths.executable('claude_cli').resolve() == second
