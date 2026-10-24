"""Packaged interpreter entry: explicit native bootstrap or existing worker script."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == '--desktop':
        from desktop.desktop_runtime import main as desktop_main
        raise SystemExit(desktop_main(args[1:]))
    if args and args[0] == '-c' and len(args) >= 2:
        sys.argv = ['-c', *args[2:]]
        exec(compile(args[1], '<command>', 'exec'), {'__name__': '__main__'})
        return
    if args and args[0] == '-m' and len(args) >= 2:
        sys.argv = args[1:]
        _ = runpy.run_module(args[1], run_name='__main__', alter_sys=True)
        return
    if not args or not Path(args[0]).is_file():
        raise SystemExit('需要明确的工作器脚本或 --desktop 启动操作')
    script = Path(args[0]).resolve()
    sys.path.insert(0, str(script.parent))
    control = script.parent.parent / 'control-plane/src'
    if (control / 'model_desk_control/__init__.py').is_file():
        sys.path.insert(0, str(control))
    sys.argv = [str(script), *args[1:]]
    if script.name == 'panel_server.py':
        from desktop.desktop_server import serve
        serve(script)
    else:
        _ = runpy.run_path(str(script), run_name='__main__')


if __name__ == '__main__':
    main()
