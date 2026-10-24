#!/usr/bin/env bash
# macOS user entrypoint. It changes no account, model, fee, project or global settings.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python="$root/.local-worker/venv/bin/python"
launcher="$root/.local-worker/open_panel.py"

if [ ! -x "$python" ]; then
  printf '模型工作台尚未安装：请先在此目录运行 ./setup.sh。\n' >&2
  exit 1
fi
if [ ! -f "$launcher" ]; then
  printf '安装目录不完整：缺少 .local-worker/open_panel.py。\n' >&2
  exit 1
fi

exec "$python" "$launcher" "$@"
