"""Installer scripts stay local, require the control kernel's Python and verify it loads."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_windows_installer_requires_python_312_and_loads_the_kernel() -> None:
    text = (ROOT / "setup.ps1").read_text(encoding="utf-8-sig")
    assert "$minimumPython = [version]'3.12'" in text
    assert "3.11" not in text
    assert "control_bootstrap.require_graph()" in text
    assert "control-plane\\src\\model_desk_control\\__init__.py" in text


def test_macos_installer_mirrors_the_local_only_default_steps() -> None:
    script = ROOT / "setup.sh"
    text = script.read_text(encoding="utf-8")
    assert script.stat().st_mode & 0o111
    assert "sys.version_info[:2] >= (3, 12)" in text
    assert "control_bootstrap.require_graph()" in text
    assert "install_control_source.py" in text
    assert "--frozen-lockfile" in text
    assert 'major===22' in text
    assert 'pnpm_version_ok' in text
    assert 'pnpm 10.26+' in text
    forbidden_default_steps = (
        "panel_server.py --port",
        "sudo ",
    )
    for forbidden in forbidden_default_steps:
        assert forbidden not in text
    assert '--register-mcp' in text
    assert '--install-rules' in text
    assert 'if [ "$register_mcp" -eq 1 ]' in text
    assert 'if [ "$install_rules" -eq 1 ]' in text
    assert 'migrate_project_storage.py --mode preview' in text
    assert '安装不会自动迁移或删除' in text


def test_codex_installer_uses_the_current_installations_posix_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = ROOT / ".local-worker/install_codex_workbench.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("portable_codex_installer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.HERE = tmp_path / ".local-worker"
    assert module.desired_server() == {
        "command": str(module.HERE / "venv/bin/python"),
        "args": [str(module.HERE / "codex_mcp_server.py")],
    }


def test_requirements_pin_the_control_kernel_dependencies() -> None:
    pins = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    for pin in ("pydantic==2.14.1", "langgraph==1.2.16", "langgraph-checkpoint-sqlite==3.1.2"):
        assert pin in pins


def test_control_source_binding_is_atomic_idempotent_and_rejects_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = ROOT / ".local-worker/install_control_source.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("portable_control_source_installer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "source"
    package = source / "model_desk_control"
    package.mkdir(parents=True)
    _ = (package / "__init__.py").write_text("", encoding="utf-8")
    site = tmp_path / "site-packages"
    site.mkdir()

    target = module.bind(source, site)
    assert target.read_text(encoding="utf-8") == f"{source.resolve()}\n"
    before = target.stat().st_mtime_ns
    assert module.bind(source, site) == target
    assert target.stat().st_mtime_ns == before

    target.unlink()
    target.symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="链接"):
        _ = module.bind(source, site)
