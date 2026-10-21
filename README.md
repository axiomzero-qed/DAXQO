# DAXQO 达序

![DAXQO app icon](assets/daxqo-app-icon.png)

[中文](#中文) | [English](#english)

## 中文

DAXQO 是面向手机的 Agent 工作台：Android 提供持续对话与成果界面，工具在手机已有的 Linux 环境执行，模型通过用户配置的 API 接入。项目采用 [MIT 许可证](LICENSE)；仓库地址为 [axiomzero-qed/DAXQO](https://github.com/axiomzero-qed/DAXQO)。第三方依赖见[第三方说明](THIRD_PARTY_NOTICES.md)。

## 快速开始

1. 在已运行的 Linux 环境中，从仓库根目录安装 DAXQO（Python 3.10+）：

   ```sh
   python3 scripts/install_linux.py
   ```

2. 下载并安装 [V0.3 Android APK](https://github.com/axiomzero-qed/DAXQO/releases/tag/v0.3.0)。这是开发签名预览包。Codex runtime 是独立依赖；Linux 安装器会从官方 npm registry 获取固定版本，或复用现有官方 runtime。默认执行策略为 `danger-full-access`，运行任务可访问该 Linux 用户可访问的文件和进程。
3. 生成一次性 Android 配对链接，并仅在自己的 DAXQO App 中导入：

   ```sh
   python3 scripts/install_linux.py --print-pairing
   ```

   将显示的链接视为秘密。Capability 只授权本机 DAXQO，不是模型 API key。StepX 用户须先让厂商 Linux 环境运行；DAXQO 不会创建或唤醒它。

4. 打开已配对的 DAXQO App，在设置中填写自己的 DeepSeek API key 并验证连接，即可创建任务。

完整选项见[中文 Linux 安装说明](docs/install.md#中文)和[English Linux install guide](docs/install.md#english)；Android 构建与安全边界见[Android 说明](docs/android-runtime.md#中文) / [Android runtime guide](docs/android-runtime.md#english)。

## V0.3 功能

- 浅色移动界面和左侧箭头品牌图标；会话、历史、设置、文件、定时任务和运行详情分区显示。
- 执行中追加要求、沿原会话继续追问、持久化消息/任务/文件；查看文本成果、下载文件、停止任务、确认工具操作及恢复中断任务。
- 通过 Android 系统文件选择器导入附件：每条消息最多 6 个、每个最多 20 MiB。文件存入手机 Linux 工作区；图片理解取决于模型与运行时，本次未完成视觉能力验收。
- 显示公开进度与运行状态，不提供隐藏思维链。
- 定时任务支持一次、每日（北京时间）和间隔执行；只有 DAXQO Linux 服务在线时才检查计划。
- 默认并发 2 个任务（可设为 1–4）；子智能体默认最多 2 个、深度 1。一次受控上下文压缩检查已完成，默认阈值为 32000 tokens、窗口为 65536 tokens；长程压力测试尚未完成。
- 模型 API key 由用户在 DAXQO GUI 中配置并保存在 Linux 端；Android 使用本机配对 capability。

V0.3 真机结果和未完成项目见[V0.3 验收记录](docs/V0.3-ACCEPTANCE.md)。此前已验证关闭 App、撤掉电脑调试通道后，手机仍能执行一次定时任务。Android App 管理已运行 Linux 环境中的 DAXQO 服务；不能创建、唤醒或保证厂商 Linux 容器持续运行，也不承诺 24 小时保活。

## 仓库结构

```text
android/           Android App 与可重复构建脚本
backend/           Python 服务、Codex App Server 适配、运行服务与测试
assets/            品牌素材
scripts/           安装、迁移、打包与本地开发工具
docs/              接口、安装、平台边界与验收说明
```

## 开发与测试

后端使用 Python 标准库；任务执行另需官方 Codex runtime 和可用模型 API。以下本地协议测试不调用模型：

```sh
cd backend
python3 -m unittest test_server test_supervisor test_runtime_control test_task_services
```

测试使用明确标记的协议/进程夹具，不替代真实手机或模型验收。Windows 若尚无 Android 构建工具，可运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -InstallTools
```

工具安装与构建细节见[Android 说明](docs/android-runtime.md#中文)。

---

## English

DAXQO is a mobile Agent workspace. Its Android app provides persistent conversations and a results interface; tools run inside a Linux environment already available on the phone, and models are connected through an API configured by the user. The project uses the [MIT License](LICENSE); its repository is [axiomzero-qed/DAXQO](https://github.com/axiomzero-qed/DAXQO). See [third-party notices](THIRD_PARTY_NOTICES.md) for external dependencies.

### Quick start

1. In an already-running Linux environment, install DAXQO from the repository root (Python 3.10 or newer):

   ```sh
   python3 scripts/install_linux.py
   ```

2. Download and install the [V0.3 Android APK](https://github.com/axiomzero-qed/DAXQO/releases/tag/v0.3.0), a development-signed preview build. Codex runtime is an independent dependency; the Linux installer fetches a pinned release from the official npm registry or reuses an existing official runtime. The default execution policy is `danger-full-access`, which lets tasks access files and processes available to that Linux user.
3. Create a one-time Android pairing link and import it only into your DAXQO app:

   ```sh
   python3 scripts/install_linux.py --print-pairing
   ```

   Treat the displayed link as a secret. Its capability authorizes the local DAXQO service and is not a model API key. On StepX, the vendor Linux environment must already be running; DAXQO cannot create or wake it.

4. Open the paired DAXQO app, enter your own DeepSeek API key in Settings, verify the connection, and create a task.

See the [English Linux installation guide](docs/install.md#english) and the [English Android runtime guide](docs/android-runtime.md#english) for details.

### V0.3 features

- A light mobile interface with the standalone left-arrow brand icon; conversations, history, settings, files, schedules, and run details are presented separately.
- Add instructions while a task is running and continue in its existing conversation. Messages, tasks, and files persist. View text results, download files, stop tasks, approve tool actions, and resume interrupted tasks.
- Import files through Android's system document picker: up to 6 files per message and 20 MiB per file. Files are stored in the phone's Linux workspace. Image understanding depends on the model and runtime and was not validated in this acceptance run.
- Show public progress and run status; hidden chain-of-thought is not exposed.
- Schedule one-time, daily (China Standard Time), or interval tasks. Schedules are checked only while the DAXQO Linux service is online.
- Default concurrency is 2 tasks (configurable from 1 to 4). Subagents default to at most 2 at depth 1. A controlled context-compaction check was completed; defaults are a 32000-token threshold and a 65536-token window. Long-duration stress testing remains incomplete.
- Users configure model API keys in the DAXQO GUI; keys remain on Linux. Android uses a local pairing capability.

See the [V0.3 acceptance record](docs/V0.3-ACCEPTANCE.md) for device results and remaining checks. Earlier acceptance confirmed that a scheduled task completed on the phone with the app closed and PC forwarding removed. The Android app manages DAXQO services inside an already-running Linux environment. It cannot create, wake, or guarantee the availability of a vendor-managed Linux container, and it does not promise 24-hour uptime.

### Repository layout

```text
android/           Android app and reproducible build scripts
backend/           Python service, Codex App Server adapter, runtime control, and tests
assets/            Brand assets
scripts/           Installation, migration, packaging, and local development tools
docs/              API, installation, platform boundaries, and acceptance notes
```

### Development and tests

The backend uses the Python standard library. Task execution additionally requires the official Codex runtime and an available model API. These local protocol tests do not call a model:

```sh
cd backend
python3 -m unittest test_server test_supervisor test_runtime_control test_task_services
```

Tests use clearly labeled protocol/process fixtures and do not replace real-device or model acceptance. On Windows, if Android build tools are not installed, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -InstallTools
```

See the [English Android runtime guide](docs/android-runtime.md#english) for build details.
