"""Start the hidden panel without inheriting the caller's terminal/pipeline handles."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import platform_runtime


def start(port=8765):
    if not 1 <= port <= 65535:
        raise ValueError('invalid panel port')
    here = Path(__file__).resolve().parent
    python = platform_runtime.worker_python(here)
    with (here / 'panel.stdout.log').open('ab', buffering=0) as stdout, (here / 'panel.stderr.log').open('ab', buffering=0) as stderr:
        process = subprocess.Popen([str(python), str(here / 'panel_server.py'), '--port', str(port)],
            cwd=here.parent, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, close_fds=True,
            **platform_runtime.process_options())
    return process.pid


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    print(json.dumps({'pid': start(args.port)}))
