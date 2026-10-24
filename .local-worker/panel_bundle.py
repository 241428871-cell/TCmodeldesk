"""Bundle declared local panel assets without services, accounts or external resources."""

import re
from pathlib import Path
from typing import Final

STYLES: Final = frozenset(
    {"style.css", "workspace.css", "governance.css", "reconciliation.css", "flow.css"}
)
SCRIPTS: Final = frozenset(
    {"app.js", "workspace.js", "reconciliation.js", "governance.js", "flow.js"}
)
STYLE: Final = re.compile(r'<link\s+rel="stylesheet"\s+href="/([^"<>]+)"\s*>')
SCRIPT: Final = re.compile(r'<script\s+src="/([^"<>]+)"\s*>\s*</script>')
EXTERNAL: Final = re.compile(
    r"<script\b[^>]*\bsrc\s*=|<link\b[^>]*\brel\s*=\s*[\"']stylesheet", re.IGNORECASE
)


class BundleError(ValueError):
    """A changed or incomplete asset set must not silently produce a broken host UI."""

    def __init__(self) -> None:
        """Return a stable failure without embedding source content."""
        super().__init__("工作台资源不完整或页面结构已变化，无法安全打包界面。")


def _read(folder: Path, name: str) -> str:
    try:
        return (folder / name).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise BundleError from error


def _script(source: str) -> str:
    escaped = re.sub(r"</(script)", r"<\\/\1", source, flags=re.IGNORECASE)
    return "<script>\n" + escaped + "\n</script>"


def bundle(folder: Path, bridge_name: str) -> str:
    """Preserve source order and put the host bridge immediately before app.js."""
    if bridge_name != "codex-bridge.js":
        raise BundleError
    index = _read(folder, "index.html")
    styles = STYLE.findall(index)
    scripts = SCRIPT.findall(index)
    if (
        styles.count("style.css") != 1
        or scripts.count("app.js") != 1
        or len(set(styles)) != len(styles)
        or len(set(scripts)) != len(scripts)
        or not set(styles) <= STYLES
        or not set(scripts) <= SCRIPTS
    ):
        raise BundleError
    bridge = _read(folder, bridge_name)

    def stylesheet(match: re.Match[str]) -> str:
        source = _read(folder, match[1])
        escaped = re.sub(r"</(style)", r"<\\/\1", source, flags=re.IGNORECASE)
        return "<style>\n" + escaped + "\n</style>"

    def script(match: re.Match[str]) -> str:
        return (_script(bridge) if match[1] == "app.js" else "") + _script(_read(folder, match[1]))

    result = SCRIPT.sub(script, STYLE.sub(stylesheet, index))
    if EXTERNAL.search(result):
        raise BundleError
    return result
