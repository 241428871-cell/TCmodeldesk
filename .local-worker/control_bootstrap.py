"""Resolve the bundled control package without starting work or installing dependencies."""

import importlib
import sys
from pathlib import Path


class ControlSetupError(ValueError):
    """An explicit action cannot load the control package for this installation."""

    def __init__(self, code: str, detail: str) -> None:
        """Retain a safe machine reason and a readable installation diagnostic."""
        self.code: str = code
        super().__init__(detail)


def bind_source() -> None:
    """Bind to this installation, rather than another checkout's installed source."""
    source = Path(__file__).resolve().parent.parent / "control-plane" / "src"
    if not (source / "model_desk_control" / "__init__.py").is_file():
        code = "control_source_missing"
        detail = "当前安装缺少控制内核源码, 请使用完整安装包. 未启动模型"
        raise ControlSetupError(code, detail)
    loaded = sys.modules.get("model_desk_control")
    if loaded is not None and (
        not loaded.__file__
        or Path(loaded.__file__).resolve()
        != (source / "model_desk_control" / "__init__.py").resolve()
    ):
        code = "control_source_mismatch"
        detail = "当前进程已加载另一份安装的控制内核, 请在独立进程中使用此安装"
        raise ControlSetupError(code, detail)
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))


def require_graph() -> None:
    """Only explicit start/resume/run calls check the optional workflow dependency."""
    bind_source()
    try:
        _ = importlib.import_module("model_desk_control.host_flow")
        _ = importlib.import_module("langgraph.checkpoint.sqlite")
    except ModuleNotFoundError as error:
        code = "control_dependency_missing"
        detail = "当前 Python 未安装锁定的 LangGraph 依赖, 请更新安装后再运行目标"
        raise ControlSetupError(code, detail) from error
