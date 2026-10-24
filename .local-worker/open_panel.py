"""Explicitly start this installation's panel and optionally open its local URL."""

from __future__ import annotations

import argparse
import json
import webbrowser

import panel_service


def open_panel(port: int, *, browser: bool) -> dict[str, object]:
    """Reuse the guarded service bootstrap; opening a browser never starts model work."""
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    result = panel_service.ensure_service(f"http://127.0.0.1:{port}")
    opened = False
    if browser:
        opened = bool(webbrowser.open(str(result["url"]), new=2, autoraise=True))
        if not opened:
            raise ValueError("工作台服务已就绪，但系统没有确认浏览器已打开")
    return {**result, "browser_opened": opened}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = open_panel(args.port, browser=not args.no_browser)
    except (OSError, ValueError) as error:
        print(json.dumps({"ready": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
