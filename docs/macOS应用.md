# TCmodeldesk macOS App

原生 AppKit 窗口承载现有工作台，由系统 WKWebView 渲染，不再打开浏览器标签。Python 后端、控制内核、Node 22 与 acpx 随应用分发。Codex／DSH 默认使用电脑上桌面应用内的后台 CLI，可切独立 CLI；Claude 与 cswap 保留原安装及账户。

## 使用

将安装镜像中的 `TCmodeldesk.app` 拖入“应用程序”，双击打开。首次使用会在用户系统盘 `~/Library/Application Support/TCmodeldesk/Data` 创建独立安装，模型连接和费用授权保持空／关闭，不复制旧安装的账户。应用不要求另外安装 Anaconda、Python 或 Node。

已有数据可从“TCmodeldesk → 连接已有工作台”选择原安装目录。只复用身份吻合的服务；不会替换其他目录的端口。选择会记住，菜单“使用应用独立工作台”可返回应用自管安装。独立安装使用系统分配的本地端口，不占用 8765／9123。关闭窗口后应用继续在程序坞和右上角菜单栏显示；点击程序坞图标或菜单“打开 TCmodeldesk”可恢复窗口。明确退出 App 时，自管后台必须通过目标、任务、租约和未结算轮次检查并正常退出，才结束窗口程序；忙碌或未知状态会阻止退出并显示原因，不强杀模型。连接外部工作台时退出仅断开窗口，不停止外部服务。切换安装也先安全停止当前自管后台，避免留下隐形服务。

旧版 TCworkdesk 的数据位置指针与现有数据目录会被兼容读取，改名不会自动搬移或建立空白替代；旧目录可继续使用，之后从迁移菜单改到新位置。菜单与新安装使用 TCmodeldesk 名称。

## 数据位置与一键迁移

“TCmodeldesk → 数据位置与迁移”显示当前位置。选择新的上级文件夹后，应用创建 `TCmodeldesk Data`，自动执行：

1. 核对启用目标、未结算主脑、任务与持久资源，任何忙碌或未知状态均拒绝迁移。
2. 请求受管后台正常退出，并核对 PID／创建时间；没有退出证明就不复制。
3. 复制到暂存目录，逐文件 SHA-256 核对原数据与副本，拒绝变化中的数据和目录外链接。
4. 发布新目录、原子保存路径设置，再启动并验证新位置。新后台启动失败则恢复原路径设置；目标副本保留。

原数据目录保留为回退副本，不自动删除。目标必须尚不存在，且不能与原目录互相包含；支持跨磁盘复制。外部磁盘不在时明确报错，不自动建立一份空数据。小型位置指针和 macOS 管理的窗口／WebKit 偏好仍在用户系统目录。

迁移范围是应用自管的设置、模型配置、全局运行数据、日志和运行环境。项目源码及项目内 `.model-desk` 记录继续随项目保存，项目身份和路径不变；系统钥匙串、Claude/cswap、DSH/Codex 外部账户不移动。连接已有安装时，迁移按钮明确拒绝搬动原安装，避免把源代码工作树误当应用数据目录。

## 构建

在 macOS 的指定 Anaconda 开发环境安装 `desktop/requirements-build.txt`，先按原安装流程准备 Node 22 与锁定 acpx 依赖，然后运行：

```sh
conda run -n aad-control python desktop/build_macos.py --output dist/macos-modeldesk
conda run -n aad-control python desktop/package_dmg.py dist/macos-modeldesk/TCmodeldesk.app dist/TCmodeldesk-0.3.1-macOS-arm64.dmg
```

构建需要 Apple Command Line Tools；用户运行 App 无需它。源码与第三方依赖通过白名单复制，不读取模型配置、密钥、会话、任务和项目数据库。运行时许可文本位于应用 Resources/licenses。当前构建为 Apple Silicon arm64，应用声明 macOS 13+；实际原生验证只覆盖本机。只有本地签名，未使用 Developer ID／未公证，不代表已完成其他 Mac 或 Intel 发行验收。

## 验收记录（2026-10-23）

- 12 项目录初始化／配置损坏／迁移校验／备份保留／危险目标拒绝测试通过，与原控制器测试合计 19 passed。
- 原生 Swift 类型检查通过，Python 桌面模块指定环境检查 0 errors／0 warnings。
- 打包解释器能加载 Python 3.12.15、控制依赖并分派子进程。最小 PATH、没有 PYTHONPATH 的环境下，完整后台冷启动和同进程复用成功，无外部 Anaconda 运行依赖。
- 打包后端实际迁移数据并启动新位置；随后从工作盘迁到系统临时盘，源／目标设备号不同，校验内容一致，原副本保留。验收只使用一次性数据。
- 原生窗口实际呈现完整工作台，原生数据菜单与“外部安装不迁移”提示已检查；退出 App 后界面进程退出。窗口验收使用隔离 HTTP 夹具，不读取真实账户。
- 最终冻结后端通过真实 HTTP → 持久租约 → 桌面内置 Luna → 完成结算，结果正确、退出码 0、租约释放、活动任务清零；费用拒绝先返回 400，只在一次性安装中临时授权，真实设置未改。另一次结果完成但资源证明未释放，门禁保留，未放宽；单独复核通过，不将其原因未经证实地归于打包。
- App 本地签名验证通过；安装镜像和本次构建的运行态回执在本机忽略目录，不上传账户或记录。

本轮复用既有项目、治理、CLI、审核、恢复和双宿主核心；原生窗口不增加新的 AI 主脑、开发续接时钟或费用授权。Qt／Electron／Tauri 未引入。实现参考 [Apple WKWebView](https://developer.apple.com/documentation/webkit/wkwebview)、[AppKit 退出生命周期](https://developer.apple.com/documentation/appkit/nsapplicationdelegate/applicationshouldterminate(_:)) 与 [PyInstaller 冻结运行信息](https://pyinstaller.org/en/stable/runtime-information.html)。

## TCmodeldesk 0.3.1 改名与退出验收

本轮 23 项桌面／控制器检查、Swift 与 Python 类型检查通过。隔离原生窗口验证关闭后恢复、忙碌退出提示与空闲退出后前后端均结束；实际冻结后端在最小 PATH 下验证冷启动、同 PID 复用、安全退出和重复停止不重启，未启动模型。菜单栏采用 NSStatusItem，程序坞使用 regular 激活策略；CUA 无法捕获系统菜单栏／Dock，二者的最终视觉与菜单点击仍需原生检查。旧自管安装继续使用其已有业务源码，当前没有自动源码升级器；连接原安装也不会覆盖其界面和数据。后续润色任务见 [Claude 界面交接](Claude界面润色.md)。
