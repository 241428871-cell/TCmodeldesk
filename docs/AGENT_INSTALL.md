# 给你的 Agent：macOS 下载、安装与首次验收

更新日期：2026-10-23。仓库：[Gambler134201/multi-agent-collaboration](https://github.com/Gambler134201/multi-agent-collaboration)。

本文交给使用者自己的 Agent 执行。当前交付主线是 macOS / POSIX；Windows 兼容文件保留，但不属于本轮支持与验收范围。安装路径、账号、模型、项目与费用授权均来自使用者，不能照抄维护者机器。

## 可直接复制的指令

```text
请在 macOS 上安装并验收：
https://github.com/Gambler134201/multi-agent-collaboration

先阅读 AGENTS.md、docs/AGENT_INSTALL.md、docs/INSTALL.md 和 docs/CROSS_HOST_FEATURES.md，并按实际源码核对命令。
先只读检查 Python 3.12+、Claude/Codex CLI、可选 Node/pnpm，以及我选定的安装目录、主脑和执行端。已有明确选择就沿用，不默认安装所有外部应用。
不要覆盖已有安装、项目记录、模型配置、账号、MCP 条目、全局规则或插件收据；不要读取、复制或输出认证文件、Token、会话和密钥。
默认只安装本目录依赖。全局 MCP、全局规则、DSH 插件、真实模型调用和付费测试都需要我的明确授权。
最后分别报告：源码清单、依赖安装、服务归属、主脑入口、执行端、真实任务、独立验收。没有实际证据的层保持“未验证”。
```

## 1. 确认选择与边界

| 能力 | 当前入口 | 安装前需要确认 |
| --- | --- | --- |
| 决策主脑 | Claude CLI 或 Codex CLI，原生 auto 权限 | 已安装 CLI、项目目录、选定模型、费用授权 |
| Codex 宿主 | 可选全局 `model_desk` MCP 和工作流规则 | 是否允许写当前用户 Codex 配置 |
| DSH 宿主 | 可选 DeepSeek Harness 桌面插件，共用同一工作台核心 | DSH profile、插件安装资料目录、是否允许安装 |
| 执行模型 | Claude/Codex CLI；可选 DSH、Pi、WorkBuddy、API/本地模型 | 对应程序、模型连接和允许范围 |
| 账户信息 | Claude/Codex 原生额度；DeepSeek API 余额 | 已登录或已配置；不可用必须显示 unknown |

首次建议只配置一个决策模型和一个执行模型。项目默认工作并发为 1，可按项目显式设置 1–4；项目、账户桶和文件租约仍会限制实际并行，不能把线程池大小当作已获并发授权。

## 2. 下载并核对来源

选择一个不存在的目标目录；不要覆盖旧安装或把工作台目录当作业务项目。

```bash
install_parent="$HOME/Applications"       # 换成使用者选定且已存在的父目录
install_root="$install_parent/multi-agent-collaboration"
test -d "$install_parent" || exit 1
test ! -e "$install_root" || exit 1
git clone --branch main https://github.com/Gambler134201/multi-agent-collaboration.git "$install_root"
cd "$install_root"
git rev-parse HEAD
```

发布版应先只读核对源码清单：

```bash
/path/to/python3.12 .local-worker/source_manifest.py --check
```

该命令从 Git 索引重新计算路径、字节数和 SHA-256，不启动服务或模型。清单过期、存在未忽略未跟踪文件、跟踪链接或异常路径都会返回非零；不能用旧清单继续声称发布包完整。`--write` 只供维护者在已审查且无未跟踪文件的发布分支上显式重建，不是安装步骤。

ZIP 安装没有 Git 索引，需记录下载来源、日期和发布提交；不能为了安装按钮临时初始化 Git 后伪造清单通过。

## 3. 安装本目录依赖

```bash
./setup.sh --check
./setup.sh --python /path/to/python3.12 --skip-node-deps
```

只使用 Claude/Codex CLI 时保留 `--skip-node-deps`。选用 DSH/Pi Node 执行器时，先确认 Node 22.x 与 pnpm，再去掉该参数。安装器只在 `.local-worker/venv` 和本目录 Node runtime 中写依赖；它不会启动面板、调用模型、开启付费权限或注册全局集成。

安装后核对：

```bash
.local-worker/venv/bin/python -m pytest .local-worker/tests/test_opensource_bootstrap.py -q
.local-worker/venv/bin/python -m pytest .local-worker/tests/test_installers.py -q
```

这些是安装合同检查，不证明真实账户、宿主 GUI 或模型调用可用。

## 4. 启动工作台并确认归属

只有使用者已经请求启动时才执行：

```bash
./打开模型工作台.command --no-browser
```

默认端口是 8765。端口已占用时先核对 PID、启动时间和安装目录，不停止不明进程。服务就绪后再打开绑定真实业务项目的页面；安装根目录不会自动成为业务项目。

安全停止使用 `.local-worker/panel-runtime.json` 中已核对的 PID，并再次核对进程创建身份。不要提供任意 HTTP shutdown 接口。

## 5. 配置主脑、项目和执行端

1. 左侧添加业务项目文件夹。
2. 在“连接与模型库”读取 Claude/Codex 原生目录或添加使用者自己的 API/本地模型。
3. 在项目下分别绑定决策模型和执行模型；设置 effort、Codex fast、autocompact 与项目并发。
4. 协作总开关和付费授权默认关闭。只有使用者明确选择后才开启；金额/调用限制不能为排障被静默关闭。
5. 新建 Goal，固定准则、允许文件和验收条件。阶段和依赖由主脑动态提交。
6. `/btw` 只读查询进展；新要求通过“插入需求”或模型可见 `goal_interact` 持久写入。`/pause`、`/stop` 走独立控制动作。

Claude/Codex auto 主脑在 Goal 的稳定隔离副本中运行；只有结构化动作经过授权、租约、写回和独立验收后进入原项目。它仍不等同完整 OS 沙箱，最终验收必须检查真实文件范围。

### 可选 Codex 全局集成

只有使用者明确允许时执行：

```bash
./setup.sh --register-mcp
./setup.sh --install-rules
```

同名冲突应停止，不覆盖其他 MCP 或非托管规则。已有聊天可能需要新建或重载。工具可见不等于规则已加载，也不自动开启协作或付费。

### 可选 DSH / Pi / WorkBuddy

- DSH 主脑插件按 [DSH 使用指南](DSH_USAGE.md) 的 prepare/check/install/receipt 流程操作；完全退出 DSH 后再安装或升级，不直接删除插件文件，不自动重启 DSH。
- DSH/Pi 执行器需要匹配版本和本地 Node runtime；状态失败时保留真实原因，不换成其他引擎。
- WorkBuddy 使用自己的安装目录、用户配置目录和明确模型；不读取认证文件，不在失败时切到 Auto 或 API Key。详见 [执行端说明](WORKBUDDY_EXECUTOR.md)。

这些外部宿主未配置时属于环境未就绪，不是允许跳过项目隔离、费用或验收合同的理由。

## 6. 首次分层验收

| 层级 | 必须看到的证据 | 不能替代它的内容 |
| --- | --- | --- |
| 来源 | 发布提交与当前 `SOURCE-MANIFEST` 核对通过 | 仅有仓库 URL |
| 安装 | venv、控制内核绑定、安装测试通过 | 仅解压成功 |
| 工作台 | 服务 PID、监听端口与安装目录一致 | 任意 8765 返回页面 |
| 项目 | 页面与记录都绑定选定绝对目录 | 最近打开项目或安装目录 |
| 主脑 | 真实原生会话、有效权限模式、结构化回执 | UI 显示“已启用” |
| 执行 | 唯一 task_id、租约、实际模型/Agent、文件与测试回执 | 执行器自报 completed |
| 恢复 | 原进程身份核对、同一任务接管且无重复模型调用 | 创建新任务重跑 |
| 验收 | 主脑或独立检查者核对实际改动与测试 | 任务退出码为零 |

真实模型测试必须先取得具体模型、费用和允许文件范围授权。建议使用一次性临时业务目录、单个小文件和固定检查；记录模型、task_id、费用可用性、文件回执、租约终态与独立验收。余额、调用数或底层费用无法读取时明确 unknown。

## 7. 升级、迁移与交付报告

旧项目记录不会自动迁移。按 `docs/INSTALL.md` 使用 `preview → dry-run → apply → verify`，活动任务、未知进程、项目冲突或链接路径会拒绝迁移；保留备份和 rollback 入口。

升级时先核对 Git 状态、队列、Goal、插件收据与服务归属。工作树不干净或来源关系不清楚时停止；不自动 stash、reset、clean 或覆盖。需要提交、推送、部署、发布清单写入或真实模型调用时，先给出具体候选和操作清单，再取得授权。

最终报告至少包含：安装路径与提交号、源码清单结果、所选主脑/执行端、实际执行命令及退出状态、用户配置变更、服务归属、各层验收、未验证项与下一步。不得包含密钥、认证内容、会话原文、账户详情或项目 `.model-desk` 记录。
