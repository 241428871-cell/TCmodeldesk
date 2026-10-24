from __future__ import annotations

from pathlib import Path

from cli_sources import dsh_package


def update_command(command: Path | None) -> tuple[str, ...] | None:
    if command is None:
        return None
    prefix = Path.home() / '.local/share/tcmodeldesk/dsh-cli'
    node_root = prefix.parent / 'node22'
    node = node_root / 'bin/node'
    npm = node_root / 'lib/node_modules/npm/bin/npm-cli.js'
    try:
        package, _ = dsh_package(command)
    except ValueError:
        return None
    if package != prefix / 'lib/node_modules/@deepseek-ai/dsh' or not node.is_file() or not npm.is_file():
        return None
    return (str(node), str(npm), 'install', '--global', '--prefix', str(prefix),
            '@deepseek-ai/dsh@latest', '--registry=https://registry.npmjs.org',
            '--no-audit', '--no-fund', '--ignore-scripts')
