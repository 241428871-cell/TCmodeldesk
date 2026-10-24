#!/usr/bin/env bash
# setup.sh - macOS installation helper for this directory only (mirrors setup.ps1's default steps).
#
#   1. report the prerequisites this script can see
#   2. create .local-worker/venv from the Python you choose (3.12+)
#   3. install requirements.txt, bind this copy's control source, and load it
#   4. optionally install the local Node dependency in .local-worker/agent-runtime
#
# Never done here: starting the panel, calling a model, dispatching work, registering a
# global MCP or rule, installing Python/Node/CLIs, or overwriting an existing venv.
# Global MCP/rules are changed only with their explicit flags.
# Usage: ./setup.sh [--check] [--python /path/to/python3.12] [--skip-node-deps]
#                   [--register-mcp] [--install-rules]
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
worker="$root/.local-worker"
venv="$worker/venv"
venv_python="$venv/bin/python"
runtime="$worker/agent-runtime"
local_node="$worker/node-runtime/bin/node"
local_pnpm="$worker/node-runtime/bin/pnpm"
requirements="$root/requirements.txt"
control="$root/control-plane/src/model_desk_control/__init__.py"
check=0
skip_node=0
python=""
register_mcp=0
install_rules=0

while [ $# -gt 0 ]; do
  case "$1" in
    --check) check=1 ;;
    --skip-node-deps) skip_node=1 ;;
    --register-mcp) register_mcp=1 ;;
    --install-rules) install_rules=1 ;;
    --python) shift; python="${1:-}" ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n== %s\n' "$1"; }
note() { printf '   %s\n' "$1"; }
warn() { printf '   [注意] %s\n' "$1"; }
fail() { printf '\n安装未完成：%s\n本脚本只在当前安装目录内创建 venv 和安装依赖。\n' "$1" >&2; exit 1; }

version_ok() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)' 2>/dev/null
}

node_version_ok() {
  "$1" -e 'const major=Number(process.versions.node.split(".")[0]);process.exit(major===22?0:1)' \
    >/dev/null 2>&1
}

pnpm_version_ok() {
  value="$(PATH="$(dirname "$node_exe"):$PATH" "$1" --version 2>/dev/null)" || return 1
  major="${value%%.*}"
  rest="${value#*.}"
  minor="${rest%%.*}"
  case "$major:$minor" in
    *[!0-9:]*|:*) return 1 ;;
  esac
  [ "$major" -gt 10 ] || { [ "$major" -eq 10 ] && [ "$minor" -ge 26 ]; }
}

for required in "$worker" "$requirements" "$control"; do
  [ -e "$required" ] || fail "安装目录不完整，缺少 ${required}。请在解压后的项目根目录运行本脚本。"
done

step '步骤 1/4 本机前提（只读取，不修改）'
candidate=""
for exe in "$python" python3.12 python3; do
  [ -n "$exe" ] || continue
  if command -v "$exe" >/dev/null 2>&1 && version_ok "$exe"; then
    candidate="$(command -v "$exe")"
    break
  fi
done
if [ -n "$candidate" ]; then
  note "Python：$candidate ($("$candidate" -c 'import platform; print(platform.python_version())'))"
else
  warn '没有可用的 Python 3.12+；控制内核使用 3.12 语法（本脚本不代装 Python）。'
fi
node_exe=""
if [ -x "$local_node" ]; then node_exe="$local_node"; elif command -v node >/dev/null 2>&1; then node_exe="$(command -v node)"; fi
pnpm_exe=""
if [ -x "$local_pnpm" ]; then pnpm_exe="$local_pnpm"; elif command -v pnpm >/dev/null 2>&1; then pnpm_exe="$(command -v pnpm)"; fi
if [ -n "$node_exe" ]; then
  note "node = $node_exe ($("$node_exe" --version 2>/dev/null || printf '版本不可读'))"
  node_version_ok "$node_exe" || warn 'Node 不是已验收的 22.x；DSH Node 运行时不会安装。'
else
  warn 'PATH 中没有 node。'
fi
if [ -n "$pnpm_exe" ]; then
  note "pnpm = $pnpm_exe ($(PATH="$(dirname "$node_exe"):$PATH" "$pnpm_exe" --version 2>/dev/null || printf '版本不可读'))"
  pnpm_version_ok "$pnpm_exe" || warn 'pnpm 低于已验收的 10.26；DSH Node 运行时不会安装。'
else
  warn 'PATH 中没有 pnpm。'
fi
for tool in claude codex; do
  if command -v "$tool" >/dev/null 2>&1; then note "$tool = $(command -v "$tool")"; else warn "PATH 中没有 ${tool}。"; fi
done
if [ -x "$venv_python" ]; then note "venv 已存在：$venv_python"; else note 'venv 尚未创建。'; fi

if [ "$check" -eq 1 ]; then
  step '检查模式结束（未写入任何文件，未启动任何服务）'
  exit 0
fi

step '步骤 2/4 创建本目录的 Python 虚拟环境'
if [ -x "$venv_python" ]; then
  note '已存在，跳过创建（不会覆盖或删除既有 venv）。'
else
  [ -n "$candidate" ] || fail '需要 Python 3.12+；请先安装，或通过 --python 指定解释器。'
  "$candidate" -m venv "$venv" || fail '创建 venv 失败；本脚本不会删除任何已有环境。'
  note "已创建 $venv_python"
fi

step '步骤 3/4 安装 requirements.txt 声明的 Python 依赖（需要联网下载）'
"$venv_python" -m pip install --disable-pip-version-check --no-input --upgrade -r "$requirements" \
  || fail 'pip 安装失败；依赖只装进本目录 venv，不涉及全局 Python。'
# A plain .pth binds this source directory without a second package copy.  The
# venv is installation-local, so rerunning setup also repairs a stale path after
# an intentional directory move.
"$venv_python" "$worker/install_control_source.py" "$root/control-plane/src" \
  || fail '无法把当前安装的控制内核绑定到本目录 venv。'
# Import the bundled control kernel in a fresh interpreter; this starts nothing.
"$venv_python" -c 'import sys; sys.path.insert(0, sys.argv[1]); import control_bootstrap; control_bootstrap.require_graph(); print("控制内核与 LangGraph 依赖可加载")' "$worker" \
  || fail '控制内核或 LangGraph 依赖无法加载；请核对 control-plane 目录与 requirements.txt 安装结果。'

step '步骤 4/4 本地 Node 依赖（.local-worker/agent-runtime，DSH 执行器使用）'
if [ "$skip_node" -eq 1 ]; then
  note '按 --skip-node-deps 跳过。'
elif [ -f "$runtime/node_modules/acpx/package.json" ]; then
  note '已存在，跳过（不会重装第三方应用）。'
elif [ -z "$node_exe" ] || ! node_version_ok "$node_exe"; then
  fail 'DSH 执行器需要已验收的 Node 22.x；本脚本不会安装或替换 Node。'
elif [ -z "$pnpm_exe" ] || ! pnpm_version_ok "$pnpm_exe"; then
  fail 'DSH 执行器需要 pnpm 10.26+；本脚本不会安装或替换 pnpm。'
else
  (cd "$runtime" && PATH="$(dirname "$node_exe"):$PATH" "$pnpm_exe" install --frozen-lockfile) || fail 'pnpm install 失败。'
  note '已按 pnpm-lock.yaml 安装。'
fi

if [ "$register_mcp" -eq 1 ]; then
  step '可选：注册全局 model_desk MCP（因为你显式使用了 --register-mcp）'
  if "$venv_python" "$worker/install_codex_workbench.py" --check; then
    note '已按当前路径注册，跳过。'
  else
    "$venv_python" "$worker/install_codex_workbench.py" \
      || fail 'Codex MCP 注册未完成；同名冲突和其他 MCP 设置均不会被覆盖。'
  fi
  warn 'MCP 属于当前用户的全局配置；已打开的 Codex 会话需要新建或重载。'
fi

if [ "$install_rules" -eq 1 ]; then
  step '可选：安装全局协作规则（因为你显式使用了 --install-rules）'
  "$venv_python" "$worker/install_workflow_rules.py" \
    || fail '全局规则安装未完成；既有非托管指令保持原样。'
  warn '规则和操作指南已绑定当前安装目录；已有会话需要新建或重载。'
fi

if [ -d "$worker/runs" ] || [ -f "$worker/stages.db" ]; then
  warn '检测到旧版全局任务／阶段记录；安装不会自动迁移或删除它们。'
  note "先预览：$venv_python $worker/migrate_project_storage.py --mode preview"
  note '确认队列空闲和项目归属后，再显式使用 --mode apply；可用 verify 与 rollback 核对或回退。'
fi

step '完成'
note '本脚本没有启动面板、没有调用模型、没有派发任务。'
if [ "$register_mcp" -eq 0 ] && [ "$install_rules" -eq 0 ]; then
  note '未修改全局 Codex 配置或规则。'
fi
note "下一步：双击 打开模型工作台.command，或运行 $venv_python $worker/open_panel.py --no-browser。"
note '协作开关与付费授权默认关闭，需在面板中显式打开。'
