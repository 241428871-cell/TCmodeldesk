"""Register the exact worker before executing its existing Python entrypoint."""

import os
import runpy
import sys
from pathlib import Path

import control_bootstrap

control_bootstrap.bind_source()
import lease_descendants
import lease_runtime


def main() -> None:
    """Arguments are built by the trusted executor, not supplied as a model action."""
    home, token, script, *arguments = sys.argv[1:]
    receipt = lease_runtime.enter(Path(home), token)
    os.environ["AAD_LEASE_HOME"] = home
    os.environ["AAD_LEASE_TOKEN"] = token
    os.environ["AAD_LEASE_SESSION"] = str(os.getpid())
    sys.argv = [script, *arguments]
    with lease_descendants.observe(Path(home), receipt):
        _ = runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
