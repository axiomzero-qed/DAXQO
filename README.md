# DAXQO 达氙

![DAXQO app icon](assets/daxqo-app-icon.png)

[中文](#中文) | [English](#english)

## V1.0.1 更新 / V1.0.1 update

V1.0.1 保留 V0.7 能力，微信操作默认关闭；必须由用户在手机控制页阅读封号及永久封号风险并勾选确认后才能开启，full 模式、短信、邮件和任务均不能代为授权。Neo 正式 release 验收通过（48 项 instrumentation、13 个真实微信拒绝请求及浮窗跨应用负测）；未操作真实账号，未发送消息。APK [DAXQO-1.0.1-arm64.apk](dist/v1.0.1/DAXQO-1.0.1-arm64.apk)：versionCode 101、versionName 1.0.1、non-debuggable、525,144,012 bytes，SHA-256 `0fe08bf4e1020b89715091849b4fd98ae94f435cda843c32074b4698f71c8581`。本次未重发 SMS/email、未碰小米，也未重跑云模型全闭环。门禁边界及验收细节见 [V1.0.1 说明](docs/V1.0.1.md)；证据在 `dist/v1.0.1/acceptance`。

V1.0.1 retains V0.7 capabilities and keeps WeChat operation off by default. The user must read the account-ban and permanent-ban risks on the phone control page and confirm; full mode, SMS, email, and tasks cannot authorize it. Neo release acceptance passed (48 instrumentation checks, 13 denied real-WeChat requests, and a cross-app floating-window negative check); no real account was operated and no message was sent. APK [DAXQO-1.0.1-arm64.apk](dist/v1.0.1/DAXQO-1.0.1-arm64.apk): versionCode 101, versionName 1.0.1, non-debuggable, 525,144,012 bytes, SHA-256 `0fe08bf4e1020b89715091849b4fd98ae94f435cda843c32074b4698f71c8581`. No SMS/email was resent, Xiaomi was untouched, and the cloud-model end-to-end flow was not rerun. See [V1.0.1 notes](docs/V1.0.1.md) for boundaries and results; evidence is under `dist/v1.0.1/acceptance`.


## V1.0 单 APK 离线运行环境 / V1.0 single-APK offline runtime

V1.0 面向 ARM64 STEPX Neo，把 Android 应用、PRoot、Ubuntu 用户态、Python 3.12、Codex 0.163.0、Motor、Trajectory Collector、邮件依赖和本机语音模型打进一个 APK。首次打开完全从 APK 在本机准备运行环境，无需 Termux、厂商 Linux 或电脑；云模型、邮件和短信等外部服务仍需网络或通信服务，本地语音识别在手机上运行。

发布包 [DAXQO-1.0.0-arm64.apk](dist/v1.0.0/DAXQO-1.0.0-arm64.apk) 为 versionCode 100、525,139,916 bytes，SHA-256 `7b5641dd213b91a07e9ff698b4843017a8eeaeeff2633f01baddb8e81a1d3249`。签名使用本机 Android debug keystore 名称对应的密钥，但 APK 已关闭调试；后续覆盖升级必须保留并继续使用同一私钥。

STEPX Neo 已通过完全断网且 Termux 停止时的首次初始化（26.02 秒）、真实模型任务及 PNG 预览、Motor Scout 6/6 页面、Collector 持久化 65 条记录、息屏锁屏定时任务和本机麦克风 ASR。取消回归通过：请求用时 0.203 秒，任务为 stopped；95 秒后未生成 end 文件，Engine 仍连接且后台 PID 保持。重启恢复也已通过：手动保存 Neo 后台权限后，第三次真实重启无需打开主界面即自动恢复，重启后新模型任务完成，迁移旧配置、任务和工作区读回通过。本轮只测 Neo，没有操作小米，也没有重发真实 SMS 或邮件。Neo 首次使用前需本人前往“系统设置 → 应用信息 → DAXQO → 耗电和后台 → 手动管理”，开启“允许自启动”“允许关联启动”“允许后台运行”，按返回保存后重新进入确认；这些系统授权不能预置进 APK，其他 Android 厂商也可能有各自设置。新安装仍需用户配置模型 API key 并按需授予 Android 权限。详见 [V1.0 Neo 验收记录](docs/V1.0-NEO-ACCEPTANCE.md)和 [V1.0 指南](docs/V1.0.md)。

Windows 发布构建需要本地 Android SDK/JDK、已固定版本的 Motor 与 Collector 源码，以及本机 Android debug 签名密钥：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_v1.ps1 -MotorSource "C:\path\to\DAXQO Motor" -CollectorSource "C:\path\to\DAXQO Trajectory Collector" -Release -Lint
```

## V0.7.0 历史版本：本地语音、短信控制与独立任务工作区 / V0.7.0 historical release: local voice, SMS control and task workspaces

V0.7.0 核心版（Android versionCode 17；保留 Collector/Motor 的组合 APK 为 versionCode 19）提供本机流式中文语音草稿、独立任务工作区、GUI 成果交付，以及默认关闭的可信号码短信任务控制。两台 Neo 与 Redmi K70 均已用国内 SIM 收到短信，并各自成功执行一项测试任务；Neo 自动发送的结果短信已在 K70 收到，DAXQO/RESULT 前缀被正确忽略。K70 的系统安全组件仍逐条要求确认短信发送，因此关闭了 K70 的短信结果回复；可信号码仍可提交任务，任务结果可在对话中查看。短信须明确选择可用的国内 SIM 并预先登记可信号码。普通邮件支持 Gmail、QQ 与网易，仍须登记可信联系人并验证邮件身份。语音转写可编辑且不会自动发送。版本说明见 [V0.7 指南](docs/V0.7.md)；历史记录见 [V0.5](docs/V0.5.md)、[V0.6](docs/V0.6.md) 和 [V0.6.2](docs/V0.6.2.md)。

V0.7.0 Core (Android versionCode 17; the combined APK retaining Collector/Motor uses versionCode 19) includes local streaming Chinese speech drafts, separate task workspaces, GUI artifact delivery, and trusted-number SMS task control that is off by default. Neo and Redmi K70 both received SMS through domestic SIMs and each successfully ran a test task. Neo automatically sent a result SMS that arrived on K70, where the DAXQO/RESULT prefix was correctly ignored. K70's system security component still asks for confirmation on each outgoing SMS, so SMS result replies are disabled there; trusted-number task intake remains available and results can be viewed in the conversation. SMS requires an explicitly selected domestic SIM and pre-registered numbers. Ordinary email supports Gmail, QQ and NetEase, with registered contacts and email identity verification still required. Speech drafts are editable and never sent automatically. See the [V0.7 guide](docs/V0.7.md) and preserved [V0.5](docs/V0.5.md), [V0.6](docs/V0.6.md), and [V0.6.2](docs/V0.6.2.md) history.

## 中文

DAXQO 使用同一 Android 主源码、backend 与 Agent 核心。默认通用 APK 包名为 `io.daxqo.app`；V1.0 本轮实测 STEPX Neo，其他机型仍需适配验收。`PlatformProfile` 按真机厂商显示相应设置提示。`-Xm` 仅用于构建旧包名 `io.daxqo.xm` 的升级兼容 APK，不是独立功能版本。见 [DAXQO for XM](docs/XM.md) 和[平台架构说明](docs/ARCHITECTURE.md)。

DAXQO 是面向手机的 Agent 工作台：Android 提供持续对话与成果界面，V1.0 的工具在 App 自带的 Linux 用户态环境执行，模型通过用户配置的 API 接入。项目采用 [MIT 许可证](LICENSE)；仓库地址为 [axiomzero-qed/DAXQO](https://github.com/axiomzero-qed/DAXQO)。第三方依赖见[第三方说明](THIRD_PARTY_NOTICES.md)。

V0.7.0 是历史版本记录：核心版 Android versionCode 17；旧 Neo 与 K70 组合包曾使用 versionCode 19。短信、语音草稿和邮件的原验收范围与限制见 [V0.7 指南](docs/V0.7.md)；更早记录见 [V0.6.2](docs/V0.6.2.md)、[V0.6.1](docs/V0.6.md)、[V0.5](docs/V0.5.md) 和 [V0.4](docs/V0.4.md)。当前 V1.0 构建使用上方命令；GitHub 现有 [V0.3 release](https://github.com/axiomzero-qed/DAXQO/releases/tag/v0.3.0) 仍是旧版 APK。

以下命令仅保留为 V0.x 核心 APK 的历史构建方式，不用于 V1.0 发布：

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1
```

默认构建通用 APK `io.daxqo.app` / `daxqo`。旧 XM 安装的覆盖升级需用 `-Xm` 构建 `io.daxqo.xm` / `daxqo-xm`；两者共享源码。Gradle 仍接受 `-Pplatform=xm|standard` 与旧 `-Pxm=true`：

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -Xm -Lint
```

## V0.x 外部 Linux 历史部署路径 / V0.x historical external-Linux setup

1. 在手机 Linux 用户环境中（例如 Neo 上的 Termux + Ubuntu；仍需 Python 3.10+），从仓库根目录安装 DAXQO。无需先启动厂商 Linux；Neo 的便携环境验收范围见[记录](docs/NEO-PORTABLE-ACCEPTANCE.md)：

   ```sh
   python3 scripts/install_linux.py
   ```

2. 可下载并安装 [V0.3 Android APK](https://github.com/axiomzero-qed/DAXQO/releases/tag/v0.3.0)，这是开发签名预览包，也是当前 GitHub release；它不包含 V0.6。V0.5 两版预编译 APK 已提供[官网直接下载](https://daxqo.com/#download)，配合 [WorkBuddy 部署指南](https://daxqo.com/install/)配置后台，无需自行编译；V0.6 新增功能需使用当前源码构建的 V0.6 APK（见上面的构建命令）。Codex runtime 是独立依赖；Linux 安装器会从官方 npm registry 获取固定版本，或复用现有官方 runtime。默认执行策略为 `danger-full-access`，运行任务可访问该 Linux 用户可访问的文件和进程。
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

GitHub 自动检查覆盖 Linux/Windows 的 Python 3.10/3.12 回归，以及默认通用 APK 的 Android 构建和 lint。已有 debug APK 时，可配置 JDK 17 的 `JAVA_HOME` 和 SDK 35 的 `ANDROID_HOME`，运行 `python3 scripts/check_android.py --platform standard`（手动检查旧 XM 兼容包用 `--platform xm`），检查 Java 回归、包名/版本、配对链接、快捷方式和唤醒入口。它不连接手机。

测试使用明确标记的协议/进程夹具，不替代真实手机或模型验收。Windows 若尚无 Android 构建工具，可运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -InstallTools
```

工具安装与构建细节见[Android 说明](docs/android-runtime.md#中文)。

---

## English

### V1.0 single-APK offline runtime

V1.0 targets ARM64 STEPX Neo and bundles the Android app, PRoot, Ubuntu userspace, Python 3.12, Codex 0.163.0, Motor, Trajectory Collector, mail dependencies, and the local speech model in one APK. On first launch, the phone prepares its runtime entirely from the APK; Termux, vendor Linux, and a PC are not required. Cloud models, email, and SMS still need network or carrier service; speech recognition runs locally.

The release APK [DAXQO-1.0.0-arm64.apk](dist/v1.0.0/DAXQO-1.0.0-arm64.apk) is versionCode 100, 525,139,916 bytes, SHA-256 `7b5641dd213b91a07e9ff698b4843017a8eeaeeff2633f01baddb8e81a1d3249`. It is non-debuggable and uses the private key associated with the existing local Android debug keystore name; retain that same key for future in-place upgrades.

STEPX Neo passed fully offline first-run setup with Termux stopped (26.02 seconds), a real model task and PNG preview, Motor Scout across 6/6 pages, 65 persisted Collector records with no pending queue, a screen-off locked scheduled task, and local microphone ASR. Cancellation regression passed: request time 0.203 seconds, state stopped, no end file after 95 seconds, Engine connected, and backend PID preserved. Reboot recovery also passed: after manually saving the Neo background settings, a third real reboot restored the service without opening MainActivity; a post-reboot model task completed, and migrated configuration, tasks, and workspace data remained readable. Before first use on Neo, go to System settings → App info → DAXQO → Battery and background → Manual, enable “Allow auto-start,” “Allow associated launch,” and “Allow background running,” press Back to save, then reopen and verify. These OS permissions cannot be pre-granted in an APK, and other Android vendors may require different settings. Only Neo was tested; Xiaomi was not touched. No real SMS or email was resent in this round. New installs also need the user's model API key and any requested Android permissions. See the [V1.0 Neo acceptance record](docs/V1.0-NEO-ACCEPTANCE.md) and [V1.0 guide](docs/V1.0.md).

Build a release APK on Windows with the pinned Motor and Collector sources and the existing local Android debug signing key:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_v1.ps1 -MotorSource "C:\path\to\DAXQO Motor" -CollectorSource "C:\path\to\DAXQO Trajectory Collector" -Release -Lint
```

### V0.7.0 historical core and legacy Android build

DAXQO uses one Android main source tree, backend, and Agent core. The default general APK is `io.daxqo.app`; this V1.0 acceptance covers STEPX Neo, and other devices still need compatibility testing. `PlatformProfile` selects setup guidance from the actual device manufacturer. `-Xm` builds the legacy `io.daxqo.xm` package only for upgrade compatibility, not as a separate feature release. See [DAXQO for XM](docs/XM.md) and the [platform architecture](docs/ARCHITECTURE.md).

DAXQO is a mobile Agent workspace. Its Android app provides persistent conversations and a results interface; V1.0 tools run inside the app's bundled Linux userspace, and models are connected through an API configured by the user. The project uses the [MIT License](LICENSE); its repository is [axiomzero-qed/DAXQO](https://github.com/axiomzero-qed/DAXQO). See [third-party notices](THIRD_PARTY_NOTICES.md) for external dependencies.

V0.7.0 is historical release information: the core used Android versionCode 17, and earlier combined Neo/K70 APKs used versionCode 19. See the [V0.7 guide](docs/V0.7.md) and preserved [V0.6.1](docs/V0.6.md), [V0.6.2](docs/V0.6.2.md), [V0.5](docs/V0.5.md), and [V0.4](docs/V0.4.md) history. The existing [V0.3 release](https://github.com/axiomzero-qed/DAXQO/releases/tag/v0.3.0) is also an older APK. Use the V1.0 build command above for the current release path.

The following command is retained only for historical V0.x core APK builds; it is not the V1.0 release build:

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1
```

The default build is the general `io.daxqo.app` / `daxqo` APK. Use `-Xm` to build `io.daxqo.xm` / `daxqo-xm` for upgrades of legacy XM installs; both builds share the same source. Gradle still accepts `-Pplatform=xm|standard` and the legacy `-Pxm=true` alias:

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -Xm -Lint
```

### V0.x historical external-Linux setup

1. Install DAXQO from the repository root in a Linux userspace on the phone (for example, Termux + Ubuntu on Neo; Python 3.10 or newer). The vendor Linux environment does not need to be started first; see the [Neo portable-environment acceptance record](docs/NEO-PORTABLE-ACCEPTANCE.md):

   ```sh
   python3 scripts/install_linux.py
   ```

2. You may download and install the [V0.3 Android APK](https://github.com/axiomzero-qed/DAXQO/releases/tag/v0.3.0), a development-signed preview and the current GitHub release; it does not include V0.6. Prebuilt V0.5 Standard and XM APKs are available from the [website](https://daxqo.com/#download); follow the [WorkBuddy deployment guide](https://daxqo.com/install/) to configure the backend without compiling an APK. The new V0.6 features require a V0.6 APK built from the current source using the command above. Codex runtime is an independent dependency; the Linux installer fetches a pinned release from the official npm registry or reuses an existing official runtime. The default execution policy is `danger-full-access`, which lets tasks access files and processes available to that Linux user.
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
