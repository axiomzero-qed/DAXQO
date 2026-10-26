# Neo 通用运行环境验收（2026-10-26）

STEPX Neo 可以通过 Termux + Ubuntu 运行同一份 DAXQO 后台和运行时，不依赖厂商 Linux。这里仍使用 Linux 用户环境，不是将 Python/Codex 改为 Android 原生实现。默认通用 APK 为 `io.daxqo.app`；小米提示按实际厂商选择。旧 `io.daxqo.xm` 只保留同源码的升级兼容打包入口。

## 真机与环境

| 项目 | 本轮配置 |
| --- | --- |
| 手机 | STEPX Neo PZ261A，Android 16，arm64 |
| 宿主 | Termux 0.118.3 / versionCode 1002，普通 Android 应用 UID；未获取 root |
| 用户环境 | proot-distro 5.10.3、PRoot 5.1.107.101、Ubuntu Noble arm64 |
| Python / 运行时 | Python 3.12.3，官方 Codex 0.163.0 linux-arm64 |
| 源码 | 当前仓库同一 `backend`；未复制品牌专用后台 |
| 服务 | 隔离端口 `127.0.0.1:19880`；独立测试 state/workspace |

Termux APK、Ubuntu rootfs 和 runtime 使用已有[下载实测](MIRRORS-CN.md)中的本地归档。APK SHA256 为 `e6265a57eb5ca363808488e3b01955958bed93bc0c8a0d281849b363b11027ec`，rootfs SHA256 为 `b68ef0444ca56ce4f00ae7e8ccfb0c553ffbc92e4a1b14ed7dd8aa18c6d68652`；runtime 在手机内按安装器预置 SHA512 核验后安全解包。

## 实际结果

- 独立 Ubuntu 内成功启动 Python、`codex --version` 及真实 app-server；DAXQO `0.7.0` 后台报告运行时已连接。
- 真实模型任务 `368f96fa-2c1c-49d5-993e-cb3239055e46` 创建并读回 `portable-proof.json`，随后通过 Android MCP 的 `android_status` 读取型号 `PZ261A`，任务完成。
- 独立读取成果文件确认标识 `NEO_TERMUX_a44c774b`、Python `/usr/bin/python3.12`、架构 `aarch64`；从 Android 宿主核对该测试进程执行于 Termux 的 PRoot loader。模型与原生桥均由手机直接连接，电脑用于安装与验收读取，没有作为模型或工具代理。
- 修正后在该环境运行全部后端回归：`Ran 246 tests ... OK (skipped=28)`。跳过项不计为功能验收；本轮未完成邮件依赖和全部功能的真机验证。
- Windows 的 server 回归：56 项，3 项按平台条件跳过，其余通过。默认通用包与旧 XM 兼容包均通过构建、Lint、Java 回归及包名、配对链接、快捷方式检查。

本地验收原始材料位于 `dist/neo-unified/`（不进入发布包），包括公开任务结果、成果独立核对结果及测试记录，不含模型凭据。

## 共享代码修复

PRoot 不提供当前内核的 `task/*/children`，且 `/proc/<pid>` 目录属主显示为 `0`，`status` 中仍为真实 Android UID。原扫描将这两种 UID 直接比较，漏掉脱离会话的子进程，导致两项清理回归失败。

共享 `server.py` 现在先在相同目录视图中筛选属主，再按真实 UID、父 PID 和内核启动时间验证进程身份。原有两项进程清理测试与新增虚拟属主回归均通过；未增加品牌分支，也未放宽进程身份验证。

## 部署与验收边界

环境准备可参考 [WorkBuddy 部署指南](WORKBUDDY-DEPLOY.md#3b-xm-首次环境准备实验性-termux--ubuntu)的 Termux/Ubuntu 步骤，Neo 已完成上述独立验证。新装旧 Termux APK 后，单独更新 curl 可能与旧 OpenSSL 不匹配；本轮补齐 OpenSSL 后恢复，配置文件选择保留。准备依赖时应同时对齐 `proot-distro`、`curl`、`openssl`，使用正常签名校验与配置文件保留策略。

正常部署仍使用共享 `scripts/install_linux.py` → supervisor → `18880` / `18882`。本轮为避免影响原系统，测试后台使用独立 state/workspace 和 `19880`，没有验收正式端口切换或自动迁移。两套后台不能同时占用正式端口；已有用户需要先备份、确认空闲，再受管切换。

Neo 原有 DAXQO、配对、历史任务和正式服务保留；厂商 Linux 未关闭，所以本轮不能称为“关停厂商 Linux 后的复测”。独立进程、文件、运行时和网络路径已确认不需要该环境。测试结束后已停止隔离后台、移除临时模型配置和配对副本、关闭电脑辅助通道，保留 Termux/Ubuntu 安装及无敏感信息的验收成果。

本轮未覆盖长时间息屏、重启自启、安全锁屏、完整 GUI 切换、邮件/SMS、全部手机控制操作或所有手机型号。Termux/PRoot 会话退出或被 Android 回收时，后台可能结束，不承诺全天保活。原有厂商 Linux 仍可作为可选宿主。

## English

STEPX Neo PZ261A ran the shared DAXQO backend and official runtime in Termux/Ubuntu, completed a real model task with an independently verified file, and read the phone model through Android MCP. After fixing PRoot process discovery, all 246 backend tests completed successfully with 28 conditional skips. Standard and legacy XM packaging checks passed. This validates the isolated portable runtime; existing production services/data were retained, and vendor Linux was not shut down. Production-port migration, long background runs and other devices remain outside this acceptance scope.
