"""Where the external runtimes live: one portable, mockable place.

Nothing here knows any particular machine.  There is no user name, no drive
letter and no editor cache directory baked in.  Every external runtime is
either declared by an explicit environment variable (``MODEL_DESK_DSH_EXE``,
``MODEL_DESK_PI_HOME``, ``MODEL_DESK_NODE_EXE``), found through a verifiable
PATH lookup, or discovered as an existing standard macOS DSH application.
When it is missing the caller gets ``None`` / an
:data:`UNSET` marker, so the workbench reports the engine as unavailable and
refuses to run instead of silently substituting another engine, version or
install location.

DSH and Pi are the user's own applications: this repository never bundles,
downloads or reinstalls them.  Only the versions this build was actually
verified against are accepted (see :data:`PI_VERSION` / :data:`ACPX_VERSION`).
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_RUNTIME = HERE / 'agent-runtime'
LOCAL_NODE = HERE / 'node-runtime' / 'bin' / 'node'

# --- user-configured locations (documented in docs/INSTALL.md) -------------
DSH_EXE_ENV = 'MODEL_DESK_DSH_EXE'
PI_HOME_ENV = 'MODEL_DESK_PI_HOME'
NODE_EXE_ENV = 'MODEL_DESK_NODE_EXE'

# --- what this build was actually verified against ------------------------
DSH_EXECUTABLE_NAME = 'DeepSeek Harness.exe'
DSH_CLI = 'resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js'
DSH_CLI_RELATIVE = 'dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js'
ACPX_VERSION = '0.19.5'                      # agent-runtime/package.json + pnpm-lock.yaml
ACPX_RUNTIME = 'node_modules/acpx/dist/runtime.js'
NODE_MIN_VERSION = (22, 13, 0)
NODE_VERSION_RE = re.compile(r'^(\d+)\.(\d+)\.(\d+)$')
PI_PACKAGE = '@earendil-works/pi-coding-agent'
PI_VERSION = '1.3.0'
PI_PACKAGE_DIR = 'npm-global/node_modules/' + PI_PACKAGE
PI_CLI = PI_PACKAGE_DIR + '/dist/bundle/cli.js'
PI_NODE = 'node/node/node.exe' if os.name == 'nt' else 'bin/node'

# Stand-in for "the user has not configured this runtime".  It is a relative
# path that never resolves to a real file, and its name is readable in the
# availability report instead of a fabricated drive letter.
UNSET = Path('MODEL-DESK-NOT-CONFIGURED')


def declared(name):
    """Absolute :class:`Path` from an environment variable, else ``None``.

    A relative value is rejected rather than interpreted against some current
    directory, because a mistyped path must surface as "not configured".
    """
    raw = str(os.environ.get(name) or '').strip().strip('"')
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else None


def which(name):
    """PATH lookup that always yields a :class:`Path` (``shutil.which`` gives str)."""
    found = shutil.which(name)
    return Path(found) if found else None


def _macos_dsh_executable(candidates=None):
    """Return an existing standard macOS app executable without launching it."""
    if sys.platform != 'darwin':
        return None
    candidates = candidates or (
        Path('/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness'),
        Path.home() / 'Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness',
    )
    return next((path for path in map(Path, candidates)
                 if path.is_file() and not path.is_symlink()), None)


def dsh_executable():
    """The user's declared, PATH-resolved or existing standard DSH executable."""
    return (declared(DSH_EXE_ENV) or which(DSH_EXECUTABLE_NAME)
            or which('DeepSeek Harness') or _macos_dsh_executable())


def dsh_headless_executable(executable=None):
    """Return the DSH Electron helper used for executor-only CLI sessions.

    Launching the main macOS app binary is allowed for the interactive desktop,
    but an AAD execution task must not activate or cover the user's console.
    Electron's signed generic Helper supports ``ELECTRON_RUN_AS_NODE=1`` and the
    packaged desktop CLI without registering the main GUI application.  A
    standard macOS bundle therefore uses that helper and fails closed when it
    is missing.  Other platforms keep their verified executable path.
    """
    target = Path(executable) if executable else dsh_executable()
    if target is None:
        return None
    if target.suffix.lower() == '.exe':
        return target
    if sys.platform != 'darwin' or target.suffix.lower() == '.exe':
        return target
    if target.parent.name != 'MacOS' or target.parent.parent.name != 'Contents':
        return None
    contents = target.parent.parent
    name = target.name
    helper = contents / 'Frameworks' / f'{name} Helper.app' / 'Contents' / 'MacOS' / f'{name} Helper'
    return helper if helper.is_file() and not helper.is_symlink() else None


def dsh_command(executable: Path | None = None) -> Path | None:
    """Return the installed official ``dsh`` command without launching it.

    The macOS Desktop distribution installs this command explicitly for
    background CLI use.  It owns the correct Electron/Node invocation and stays
    version-locked to the application, so callers must prefer it over rebuilding
    the private invocation from paths inside ``app.asar``.
    """
    declared_command = declared('MODEL_DESK_DSH_CLI') or which('dsh')
    if declared_command and declared_command.is_file():
        return declared_command
    target = Path(executable) if executable else dsh_executable()
    if target is None:
        return None
    if target.suffix.lower() == '.exe':
        return target
    if target.parent.name == 'MacOS' and target.parent.parent.name == 'Contents':
        command = target.parent.parent / 'Resources/runtime/cli/bin/dsh'
        return command if command.is_file() and os.access(command, os.X_OK) else None
    return None


def dsh_archive(executable=None):
    """The ASAR path implied by the verified Windows or macOS executable layout."""
    exe = Path(executable) if executable else dsh_executable()
    if exe is None:
        return None
    if exe.parent.name == 'MacOS' and exe.parent.parent.name == 'Contents':
        return exe.parent.parent / 'Resources' / 'app.asar'
    return exe.parent / 'resources' / 'app.asar'


def dsh_cli(executable=None):
    """The ACP entrypoint inside the installed DSH package, or ``None``."""
    archive = dsh_archive(executable)
    return archive / DSH_CLI_RELATIVE if archive else None


def node_executable():
    """Node used by local acpx/Pi: explicit override, install-local runtime, PATH."""
    local = LOCAL_NODE if LOCAL_NODE.is_file() and not LOCAL_NODE.is_symlink() else None
    return declared(NODE_EXE_ENV) or local or which('node')


def node_version(executable=None):
    """Return a validated Node semantic version without using a shell."""
    node = Path(executable) if executable else node_executable()
    if node is None or not node.is_file():
        return ''
    try:
        result = subprocess.run(
            [str(node), '-p', 'process.versions.node'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    value = result.stdout.strip() if result.returncode == 0 else ''
    return value if NODE_VERSION_RE.fullmatch(value) else ''


def node_supported(version):
    """Whether *version* satisfies the acpx 0.19.5 production floor."""
    match = NODE_VERSION_RE.fullmatch(str(version or ''))
    return bool(match and tuple(map(int, match.groups())) >= NODE_MIN_VERSION
                and int(match.group(1)) == NODE_MIN_VERSION[0])


def acpx_runtime(runtime_dir=None):
    """The pinned acpx runtime inside this installation (never installed here)."""
    return Path(runtime_dir or AGENT_RUNTIME) / ACPX_RUNTIME


def acpx_version(runtime_dir=None):
    """Installed acpx version, or ``''`` when the local dependency is absent."""
    try:
        manifest = json.loads((Path(runtime_dir or AGENT_RUNTIME) / 'node_modules/acpx/package.json')
                              .read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ''
    return str(manifest.get('version') or '')


def pi_home():
    """Root of the user's own Pi installation, or ``None`` when unset."""
    return declared(PI_HOME_ENV)


def pi_package(home=None):
    home = Path(home) if home else pi_home()
    if not home:
        return None
    configured = declared('MODEL_DESK_PI_PACKAGE')
    if configured:
        return configured
    candidates = [home / PI_PACKAGE_DIR, home / 'lib/node_modules' / PI_PACKAGE,
                  home / 'node_modules' / PI_PACKAGE]
    return next((path for path in candidates if path.is_dir()), candidates[0])


def pi_cli(home=None):
    package = pi_package(home)
    return package / 'dist/bundle/cli.js' if package else None


def pi_node(home=None):
    home = Path(home) if home else pi_home()
    if os.name != 'nt':
        return node_executable()
    return home / PI_NODE if home else None


def pi_release(package=None):
    """``(package name, version)`` of a Pi package directory.

    Defaults to the configured install.  An unreadable or missing install
    reports ``('', '')``: the caller then reports the engine as unverified
    instead of inventing a version.
    """
    package = Path(package) if package else pi_package()
    if package is None:
        return '', ''
    try:
        manifest = json.loads((package / 'package.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return '', ''
    return str(manifest.get('name') or ''), str(manifest.get('version') or '')
