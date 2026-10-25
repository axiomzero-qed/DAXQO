# DAXQO 达氙

![DAXQO app icon](assets/daxqo-app-icon.png)

[中文](#中文) | [English](#english)


## V0.7.0 本地语音、短信控制与独立任务工作区 / V0.7.0 local voice, SMS control and task workspaces

V0.7.0 核心版（Android versionCode 17）提供本机流式中文语音草稿、独立任务工作区、GUI 成果交付，以及默认关闭的可信号码短信任务控制。短信须选择明确识别的国内 SIM 卡槽并登记、启用可信号码；未知归属和英国 SIM 不可选。短信权限沿用手机控制授权模式，短信正文可作为任务，结果简讯可选并带 DAXQO/RESULT 前缀。普通邮件支持 Gmail、QQ 与网易，仍须登记可信联系人并验证邮件身份。语音识别在本机运行，转写可修改且不会自动发送。短信真机验收待补；旧版本历史见 [V0.5](docs/V0.5.md)、[V0.6](docs/V0.6.md) 和 [V0.6.2](docs/V0.6.2.md)，当前功能见 [V0.7 指南](docs/V0.7.md)。

V0.7.0 Core (Android versionCode 17) includes local streaming Chinese speech drafts, separate task workspaces, GUI artifact delivery, and trusted-number SMS task control that is off by default. SMS requires an explicitly identified domestic SIM slot and pre-registered, enabled numbers; unknown and UK SIMs cannot be selected. SMS permissions follow the existing phone-control authorization mode. Message text can become a task, and optional result texts use the DAXQO/RESULT prefix. Ordinary email supports Gmail, QQ and NetEase, with registered contacts and email identity verification still required. Speech recognition runs locally; drafts are editable and never sent automatically. Real-device SMS acceptance is pending. See the [V0.7 guide](docs/V0.7.md) and preserved [V0.5](docs/V0.5.md), [V0.6](docs/V0.6.md), and [V0.6.2](docs/V0.6.2.md) history.

## 中文

DAXQO 是单仓库双平台配置：Standard 为基准版本（基准提交 `8374d89`），XM 小米版共享同一 Android 主源码、backend 与 Agent 核心，仅使用独立包名、配对 scheme 和平台提示。现有功能沿用共享核心。XM 说明见 [DAXQO for XM](docs/XM.md)；架构见[平台架构说明](docs/ARCHITECTURE.md)。

DAXQO 是面向手机的 Agent 工作台：Android 提供持续对话与成果界面，工具在手机已有的 Linux 环境执行，模型通过用户配置的 API 接入。项目采用 [MIT 许可证](LICENSE)；仓库地址为 [axiomzero-qed/DAXQO](https://github.com/axiomzero-qed/DAXQO)。第三方依赖见[第三方说明](THIRD_PARTY_NOTICES.md)。

当前源码为 V0.7.0 核心版（Android versionCode 17），包含本地语音草稿、独立任务工作区、GUI 成果交付、可信邮箱普通邮件与短信任务控制。XM 与 Standard 共享核心；独立仓库的 Android Collector（versionCode 18）和 Motor（versionCode 19）可作为原生扩展叠加。短信默认关闭，需明确选择国内 SIM 并预先登记号码；V0.7 真机短信验收待填写。V0.6.2 的语音麦克风验收记录保留在 [V0.6.2 指南](docs/V0.6.2.md)。完整功能与限制见 [V0.7 指南](docs/V0.7.md)，历史见 [V0.6.1](docs/V0.6.md)、[V0.6.2](docs/V0.6.2.md)、[V0.5](docs/V0.5.md) 和 [V0.4](docs/V0.4.md)。GitHub 上现有的 [V0.3 release](https://github.com/axiomzero-qed/DAXQO/releases/tag/v0.3.0) 仍是 V0.3 APK，不包含 V0.7。构建当前源码请使用：

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1
```

XM APK 使用 `io.daxqo.xm` / `daxqo-xm`；Standard 默认使用 `io.daxqo.app` / `daxqo`。可用 `-Xm` 构建 XM，或用 Gradle 显式别名 `-Pplatform=xm|standard`；旧 `-Pxm=true` 仍兼容：

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -Xm -Lint
```

## 快速开始

1. 在手机已有且已运行的 Linux 环境中（StepX Neo 用户需先启动厂商 Linux），从仓库根目录安装 DAXQO（Python 3.10+）：

   ```sh
   python3 scripts/install_linux.py
   ```

2. 可下载并安装 [V0.3 Android APK](https://github.com/axiomzero-qed/DAXQO/releases/tag/v0.3.0)，这是开发签名预览包，也是当前 GitHub release；它不包含 V0.6。要使用当前源码功能，请从当前源码构建 APK（见上面的构建命令）。Codex runtime 是独立依赖；Linux 安装器会从官方 npm registry 获取固定版本，或复用现有官方 runtime。默认执行策略为 `danger-full-access`，运行任务可访问该 Linux 用户可访问的文件和进程。
3. 生成一次性 Android 配对链接，并仅在自己的 DAXQO App 中导入：

   ```sh
   python3 scripts/install_linux.py --print-pairing
   ```

   XM 构建需指定 `--pairing-scheme daxqo-xm`。将显示的链接视为秘密。Capability 只授权本机 DAXQO，不是模型 API key。DAXQO 不会创建或唤醒厂商 Linux 环境。Standard 与 XM 共用 `install_linux.py` → supervisor → 本机 `18882`/`18880` 服务路径。

4. 打开已配对的 DAXQO App，在设置中填写自己的 DeepSeek API key 并验证连接，即可创建任务。

完整选项见[中文 Linux 安装说明](docs/install.md#中文)和[English Linux install guide](docs/install.md#english)；Android 构建与安全边界见[Android 说明](docs/android-runtime.md#中文) / [Android runtime guide](docs/android-runtime.md#english)。

## V0.3 已发布功能 / Released V0.3 features

- 浅色移动界面和左侧箭头品牌图标；会话、历史、设置、文件、定时任务和运行详情分区显示。
- 执行中追加要求、沿原会话继续追问、持久化消息/任务/文件；查看文本成果、下载文件、停止任务、确认工具操作及恢复中断任务。
- 通过 Android 系统文件选择器导入附件：每条消息最多 6 个、每个最多 20 MiB。文件存入手机 Linux 工作区；图片理解取决于模型与运行时，本次未完成视觉能力验收。
- 显示公开进度与运行状态，不提供隐藏思维链。
- 定时任务支持一次、每日（北京时间）和间隔执行；只有 DAXQO Linux 服务在线时才检查计划。
- 默认并发 2 个任务（可设为 1–4）；子智能体默认最多 2 个、深度 1。一次受控上下文压缩检查已完成，默认阈值为 32000 tokens、窗口为 65536 tokens；长程压力测试尚未完成。
- 模型 API key 由用户在 DAXQO GUI 中配置并保存在 Linux 端；Android 使用本机配对 capability。

V0.3 真机结果和未完成项目见[V0.3 验收记录](docs/V0.3-ACCEPTANCE.md)。此前已验证关闭 App、撤掉电脑调试通道后，手机仍能执行一次定时任务。Android App 管理已运行 Linux 环境中的 DAXQO 服务；不能创建、唤醒或保证厂商 Linux 容器持续运行，也不承诺 24 小时保活。

## V0.4 历史与继承功能 / V0.4 history and inherited features

V0.4 在现有手机 Linux 环境上增加原生手机控制及邮件远程控制入口；这些能力由 V0.5 和 V0.6 继承。手机控制提供默认 `confirm` 和 `full` 模式；`full` 只允许 DAXQO 尝试 Android 已许可的操作，不会提升系统权限。`confirm` 下敏感写操作需一次性批准，可在 App 审批卡或可信邮箱确认，批准后原任务自动继续。网页仅显示模式，不切换模式；请在手机顶栏“手机控制”页设置。辅助功能默认关闭，远程邮件需单独配置邮箱和联系人。可信联系人拥有 Harness 权限，并非隔离沙箱。邮件审批和息屏续跑闭环已在真机通过，验收范围仅为 Settings App info 的 Back；其他设置、安装权限点击和支付未测试。设备限制见[V0.4 指南](docs/V0.4.md)。

可信邮箱普通邮件自动处理默认关闭；配置并启用后，仅处理预先登记且启用的联系人新邮件，并先验证邮件身份。界面列出的提供商包括 Gmail、QQ（`qq.com` / `foxmail.com`）和网易（`163.com` / `126.com` / `yeah.net`）。主题和正文一同作为任务输入，正文为空时用主题；完成结果会回信。联系人不会因来信自动添加。敏感手机写操作会等待一次确认，可在 App 审批卡或可信邮箱按正文首行回复 `确认 <UUID>` / `拒绝 <UUID>`；唯一一项有效待批时可仅回复 `确认`，不能只用主题确认。附件邮件整封拒绝。2026-10-23 04:04:57–04:06:18 的真机闭环是 Gmail：纯标题来信创建任务，确认后同一 thread 息屏唤醒并消费原审批，Settings App info 页 Back 执行一次，任务完成且最终回信送达；其他设置、安装权限点击和支付未测试。QQ/网易真实闭环尚未实测。流程见[可信邮箱普通邮件指南](docs/EMAIL-PROVIDERS.md)。

## 仓库结构

轨迹采集器和 Motor 分别在独立开源仓库维护，其他 Harness 可通过 API、CLI 或 MCP 复用。版本和可选原生扩展见[独立工具说明](docs/OPTIONAL-TOOLS.md)。

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
python3 -m unittest test_server test_supervisor test_runtime_control test_task_services test_android_mcp test_phone_backends test_remote_protocol test_email_control test_remote_email_mcp test_agent_context test_assistant_mcp
```

MCP CLI 与 Skill 安装器测试在仓库根目录运行：

```sh
python3 scripts/test_assistant_client.py
python3 scripts/test_install_assistant_skill.py
```

GitHub 自动检查覆盖 Linux/Windows 的 Python 3.10/3.12 回归，以及 Standard/XM 的 Android 构建和 lint。已有 debug APK 时，可配置 JDK 17 的 `JAVA_HOME` 和 SDK 35 的 `ANDROID_HOME`，运行 `python3 scripts/check_android.py --platform standard`（XM 用 `--platform xm`），检查五组 Java 回归、包名/版本、配对链接、快捷方式和唤醒入口。它不连接手机。

测试使用明确标记的协议/进程夹具，不替代真实手机或模型验收。Windows 若尚无 Android 构建工具，可运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -InstallTools
```

工具安装与构建细节见[Android 说明](docs/android-runtime.md#中文)。

---

## English

DAXQO is one repository with two platform configurations. Standard is the baseline (baseline commit `8374d89`); XM shares the Android main source, backend, and Agent core, with its own application ID, pairing scheme, and platform guidance. Existing features use the shared core. See [DAXQO for XM](docs/XM.md) and the [platform architecture](docs/ARCHITECTURE.md).

DAXQO is a mobile Agent workspace. Its Android app provides persistent conversations and a results interface; tools run inside a Linux environment already available on the phone, and models are connected through an API configured by the user. The project uses the [MIT License](LICENSE); its repository is [axiomzero-qed/DAXQO](https://github.com/axiomzero-qed/DAXQO). See [third-party notices](THIRD_PARTY_NOTICES.md) for external dependencies.

The current source is V0.7.0 Core (Android versionCode 17), with local speech drafts, separate task workspaces, GUI artifact delivery, trusted ordinary email, and SMS task control. XM and Standard share the core; Android Collector (versionCode 18) and Motor (versionCode 19) from their independent repositories can be layered as native extensions. SMS is off by default and requires an explicitly selected domestic SIM and pre-registered numbers; V0.7 real-device SMS acceptance is pending. The V0.6.2 microphone acceptance record remains in its [guide](docs/V0.6.2.md). See the [V0.7 guide](docs/V0.7.md), preserved [V0.6.1](docs/V0.6.md), [V0.6.2](docs/V0.6.2.md), [V0.5](docs/V0.5.md), and [V0.4](docs/V0.4.md) history. The existing [V0.3 release](https://github.com/axiomzero-qed/DAXQO/releases/tag/v0.3.0) remains a V0.3 APK and does not include V0.7. To build the current source:

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1
```

The XM APK uses `io.daxqo.xm` / `daxqo-xm`; Standard defaults to `io.daxqo.app` / `daxqo`. Build XM with `-Xm`, or use the explicit Gradle alias `-Pplatform=xm|standard`; the existing `-Pxm=true` alias remains supported:

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -Xm -Lint
```

### Quick start

1. Install DAXQO from the repository root in a Linux environment already available and running on the phone (StepX Neo users must start the vendor Linux environment first; Python 3.10 or newer):

   ```sh
   python3 scripts/install_linux.py
   ```

2. You may download and install the [V0.3 Android APK](https://github.com/axiomzero-qed/DAXQO/releases/tag/v0.3.0), a development-signed preview and the current GitHub release; it does not include V0.6. To use the current source features, build an APK from the current source using the command above. Codex runtime is an independent dependency; the Linux installer fetches a pinned release from the official npm registry or reuses an existing official runtime. The default execution policy is `danger-full-access`, which lets tasks access files and processes available to that Linux user.
3. Create a one-time Android pairing link and import it only into your DAXQO app:

   ```sh
   python3 scripts/install_linux.py --print-pairing
   ```

   For the XM build, add `--pairing-scheme daxqo-xm`. Treat the displayed link as a secret. Its capability authorizes the local DAXQO service and is not a model API key. DAXQO cannot create or wake vendor Linux. Standard and XM share the `install_linux.py` → supervisor → local `18882`/`18880` service path.

4. Open the paired DAXQO app, enter your own DeepSeek API key in Settings, verify the connection, and create a task.

See the [English Linux installation guide](docs/install.md#english) and the [English Android runtime guide](docs/android-runtime.md#english) for details.

### V0.3 released features

- A light mobile interface with the standalone left-arrow brand icon; conversations, history, settings, files, schedules, and run details are presented separately.
- Add instructions while a task is running and continue in its existing conversation. Messages, tasks, and files persist. View text results, download files, stop tasks, approve tool actions, and resume interrupted tasks.
- Import files through Android's system document picker: up to 6 files per message and 20 MiB per file. Files are stored in the phone's Linux workspace. Image understanding depends on the model and runtime and was not validated in this acceptance run.
- Show public progress and run status; hidden chain-of-thought is not exposed.
- Schedule one-time, daily (China Standard Time), or interval tasks. Schedules are checked only while the DAXQO Linux service is online.
- Default concurrency is 2 tasks (configurable from 1 to 4). Subagents default to at most 2 at depth 1. A controlled context-compaction check was completed; defaults are a 32000-token threshold and a 65536-token window. Long-duration stress testing remains incomplete.
- Users configure model API keys in the DAXQO GUI; keys remain on Linux. Android uses a local pairing capability.

See the [V0.3 acceptance record](docs/V0.3-ACCEPTANCE.md) for device results and remaining checks. Earlier acceptance confirmed that a scheduled task completed on the phone with the app closed and PC forwarding removed. The Android app manages DAXQO services inside an already-running Linux environment. It cannot create, wake, or guarantee the availability of a vendor-managed Linux container, and it does not promise 24-hour uptime.

### V0.4 history and inherited features

V0.4 added native phone control and email remote control on the existing phone Linux environment; these features are inherited by V0.5 and V0.6. Phone Control offers default `confirm` and `full` modes; `full` only lets DAXQO attempt actions Android already permits and does not elevate system permissions. In `confirm`, sensitive writes require one-time approval from the app approval card or a trusted mailbox; approval resumes the original task. The web UI displays but cannot change the mode; use Phone Control in the phone app’s top bar. Accessibility remains off by default, and email requires separate mailbox/contact setup. Trusted contacts receive Harness permissions, not an isolated sandbox. The email approval and screen-off continuation loop passed on-device for Back on Android Settings App info. Other settings, installation-permission taps, and payment actions were not tested. See the [V0.4 guide](docs/V0.4.md).

Trusted-mailbox ordinary email handling is off by default. After setup and explicit enablement, only new messages from pre-registered, enabled contacts are considered, and mail identity is verified before a task is accepted. The interface lists Gmail, QQ (`qq.com` / `foxmail.com`), and NetEase (`163.com` / `126.com` / `yeah.net`). Subject and body are combined as task input; a blank body uses the subject alone, and completed results are returned by email. Incoming mail never adds a contact automatically. Sensitive phone writes wait for one-time approval in the app or through a trusted-mailbox reply whose first body line is `确认 <UUID>` or `拒绝 <UUID>`; bare `确认` is allowed only when exactly one valid approval is pending. The subject alone is never accepted, and messages with attachments are rejected. The real-device closed-loop record from 2026-10-23 04:04:57–04:06:18 used Gmail: a subject-only email created a task; approval resumed the same thread while the phone was screen-off, consumed the original approval, performed Back on Settings App info, completed, and sent the final report. Other settings, installation-permission taps, and payment actions were not tested. Real QQ/NetEase closed loops have not been tested. See the [trusted mailbox ordinary email guide](docs/EMAIL-PROVIDERS.md).

### Repository layout

Trajectory Collector and Motor are maintained in independent open-source repositories and can be reused by other harnesses through API, CLI or MCP. See [optional tools](docs/OPTIONAL-TOOLS.md) for versions and native extensions.

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
python3 -m unittest test_server test_supervisor test_runtime_control test_task_services test_android_mcp test_phone_backends test_remote_protocol test_email_control test_remote_email_mcp test_agent_context test_assistant_mcp
```

Run the MCP CLI and Skill installer tests from the repository root:

```sh
python3 scripts/test_assistant_client.py
python3 scripts/test_install_assistant_skill.py
```

Tests use clearly labeled protocol/process fixtures and do not replace real-device or model acceptance. On Windows, if Android build tools are not installed, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -InstallTools
```

See the [English Android runtime guide](docs/android-runtime.md#english) for build details.
