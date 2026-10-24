# 第三方依赖与外部应用

## 原项目

TCmodeldesk（原名 TCworkdesk）基于 [Gambler134201/multi-agent-collaboration](https://github.com/Gambler134201/multi-agent-collaboration) 重构。原源码采用 MIT，Copyright (c) 2026 Gambler134201；完整许可保留在 [LICENSE](LICENSE)。本仓库的后续重构、原生窗口与分发不代表上游官方发行。

源码分发不附带运行环境、账户或记录。新增 macOS App 构建会单独打包 Python、Node 与后端依赖，并把安装分发中可取得的许可文本放在 `Contents/Resources/licenses`。不会打包 Codex、DSH、Claude、cswap、Pi、WorkBuddy 应用或这些应用的用户数据。

## 直接依赖

| 依赖 | 固定版本 | 安装来源 |
| --- | --- | --- |
| anyio | 4.12.1 | PyPI |
| mcp | 1.30.0 | PyPI |
| psutil | 7.2.2 | PyPI |
| tiktoken | 0.12.0 | PyPI |
| PyYAML | 6.0.3 | PyPI |
| acpx | 0.19.5 | npm registry，通过 pnpm-lock.yaml 安装 |

Python 清单见 requirements.txt；Node 直接依赖与传递依赖见 .local-worker/agent-runtime/package.json 及 pnpm-lock.yaml。
依赖自身的许可与声明随对应软件包提供，本项目的许可证不会替代它们。

## 用户单独安装

DSH（DeepSeek Harness）、Pi Agent、Codex、WorkBuddy、Python、Node 和 pnpm 由使用者独立安装。本项目只提供适配代码，未将它们的程序、SDK安装目录、原生探测目录或用户配置复制到源码包。

macOS App 用户无需另装 Python／Node；源码开发及构建仍使用独立环境。PyInstaller 6.22.3 用于生成后端，并随构建收集其 bootloader 许可和例外条款；原生窗口使用 macOS 系统 AppKit／WebKit，不复制系统框架。App 当前仅做本机签名，Developer ID 签名与公证不包含在本次本机构建中。

WorkBuddy 适配使用用户本机已安装应用提供的原生能力；账户、积分、模型访问和第三方服务条件由对应平台管理，不随本项目的 MIT 许可授予。

本项目自身源码采用 [MIT 许可证](LICENSE)，Copyright (c) 2026 Gambler134201。此许可不替代上述第三方依赖或外部应用的许可。
