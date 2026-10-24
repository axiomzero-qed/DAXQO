# DAXQO 达氙 v0.3.0

## 中文

DAXQO 的首个公开源码预览版：Android 提供对话界面，Agent 工具在手机已有的 Linux 环境运行，通过用户自己的 DeepSeek API 调用模型。

- 浅色界面，应用图标使用项目提供的原始图片。
- 执行中对话、持久历史、成果查看与下载。
- 默认并行 2 个任务，可设为 1–4 个；提供原生子智能体及 DeepSeek 消息兼容设置。
- 公开进度、工具状态、活动时间线、上下文使用与自动压缩。
- 图片与文件导入：每条消息最多 6 个、每个 20 MiB。
- 本机一次、每日北京时间或间隔定时任务，含暂停、运行和历史界面。
- 项目文档中英双语，DAXQO 自有代码采用 MIT 许可。

**下载**

- `DAXQO-0.3.0-debug.apk`：开发签名 Android 预览包。
- `DAXQO-0.3.0-source.zip`：源码包，包含 Android、后端、安装脚本和双语文档。
- `SHA256SUMS.txt`：上述文件的 SHA-256 摘要。

安装与运行需手机已有且正在运行的 Linux 环境和用户自己的模型 API 账号；APK 不包含 Linux 容器、Codex 可执行文件或模型凭据。初步适配 StepX PZ261A / Android 16。详见 [安装说明](https://github.com/axiomzero-qed/DAXQO/blob/v0.3.0/docs/install.md)。

此前手机验收的 66 项检查、并行任务、子智能体、定时、文件导入、受控压缩与移除电脑调试通道后的运行结果见[验收记录](https://github.com/axiomzero-qed/DAXQO/blob/v0.3.0/docs/V0.3-ACCEPTANCE.md)。本次发布准备只更新原图图标、双语文档和开源资料，并构建 APK；按用户要求未重跑测试，也未操作正在使用的手机。图片理解和全天保活未验收，APK 不能自行唤醒厂商 Linux 容器。

## English

DAXQO's first public source preview provides an Android conversation interface while Agent tools run in a Linux environment already available on the phone, using the user's own DeepSeek API account.

- A light interface and the project's original image as the app icon.
- In-task conversations, persistent history, artifact viewing, and downloads.
- Default concurrency of 2 tasks, configurable from 1 to 4; native subagents with DeepSeek message compatibility settings.
- Public progress, tool status, activity timelines, context usage, and automatic compaction.
- Image and file imports: up to 6 files per message, 20 MiB per file.
- Local one-time, daily China Standard Time, or interval schedules with pause, run, and history controls.
- Bilingual Chinese/English documentation. DAXQO's own code is MIT-licensed.

**Downloads**

- `DAXQO-0.3.0-debug.apk`: a development-signed Android preview build.
- `DAXQO-0.3.0-source.zip`: Android and backend source, installation scripts, and bilingual documentation.
- `SHA256SUMS.txt`: SHA-256 digests for these files.

Installation requires an existing, running Linux environment on the phone and the user's own model API account. The APK does not bundle a Linux container, the Codex executable, or model credentials. The initial device adaptation targets StepX PZ261A / Android 16. See the [installation guide](https://github.com/axiomzero-qed/DAXQO/blob/v0.3.0/docs/install.md).

The [acceptance record](https://github.com/axiomzero-qed/DAXQO/blob/v0.3.0/docs/V0.3-ACCEPTANCE.md) documents the earlier 66 checks on phone Linux, concurrency, subagents, schedules, imports, controlled compaction, and operation after removing PC forwarding. This release-preparation pass only updates the original-image icon, bilingual documentation, and open-source materials and builds the APK. At the user's request, no tests were rerun and the phone in use was left untouched. Image understanding and all-day uptime remain unvalidated; the APK cannot wake a vendor-managed Linux container.
