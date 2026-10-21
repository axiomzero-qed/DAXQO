# DAXQO Android 入口与 StepX 运行桥

## 中文

### Android App

`android/` 是 Java 原生 Android 应用，包名 `io.daxqo.app`，显示名“达序 DAXQO”，没有第三方 UI 依赖。主界面在 WebView 中加载固定地址 `http://127.0.0.1:18880/`。WebView 只允许此 HTTP loopback 主机和端口；其他顶层页面交给系统浏览器，外部子资源拦截。带 user-info、过长 URL 或规范化后含凭据参数的外链会被拦截。

应用没有通用 JavaScript 原生执行桥，只暴露窄化的 `window.DAXQOFiles.downloadFile(relativePath)`。它仅在可信 loopback GUI 顶层页面可用，只接受相对路径，并固定请求 `/api/files/download`；不向页面暴露 capability，不支持任意 URL、文件路径或命令执行。文件访问关闭、混合内容关闭、Safe Browsing 开启。HTTP 客户端禁用重定向，避免 capability 被带到非 loopback 地址。

Android 10+ 将下载保存到系统 Downloads 中的 `DAXQO/`（MediaStore），单文件最多 20 MiB。Android 8/9 回退到 App 专属 Downloads 目录，不请求广泛存储权限。网页遇到 WebView 不支持的 `blob:` 下载时可调用上述窄接口。网页上传使用 Android 系统文档选择器，可选多个文件，授予所选 URI 的临时读取权限；不申请广泛存储权限。每条消息最多 6 个附件、每个最多 20 MiB，上传到本机 Linux workspace。模型是否能理解图片取决于所选模型和 runtime；附件导入成功不代表视觉理解已验证。

Linux 侧可生成一次性配对链接 `daxqo://connect#token=<capability>`。Android 只接受此 scheme/host，校验 token 长度和字符后写入 App 私有 SharedPreferences，并保存其 SHA-256 指纹，拒绝同一安装中重复导入。随后将 token 放在 GUI 地址 `#token=…` fragment 中。Fragment 不会随初始 HTTP 请求发送；GUI 读取 token 用于现有 API 认证后清理地址。Capability 不是模型 API key，APK、源码、日志和外部 URL 不含固定 capability。新链接会覆盖本机已保存的 token；清除 App 数据或重装会清除本地的一次性记录，需在 Linux 侧为新设备/重装重新配对。

连接区只在检查连接或失败时显示；GUI 在线后隐藏。配对后冷启动先检查运行桥状态；backend 未连接时请求启动并最多轮询 20 秒，确认就绪后再载入 GUI。连接区支持启动/重试与重启；有活动任务时运行桥以 HTTP 409 拒绝重启。运行桥不可达时明确提示 Linux 环境离线。WebView 顶层地址状态由 UI 线程维护；下载 JS 接口只读该状态，不从 Java Bridge 线程访问 WebView。

V0.3 默认并发 2 个任务（可调 1–4）；Codex 默认最多 2 个子智能体，深度 1。公开进度显示阶段、摘要和运行状态，不暴露隐藏思维链。默认自动压缩阈值 32000 tokens、上下文窗口 65536 tokens。曾以临时 8000/16000 设置完成受控压缩检查，随后恢复默认；长时间、高负载测试仍未完成。一次验收中 catalog v2 出现子智能体消息丢失；catalog v1、关闭 search 后的新会话验证成功。已有会话保留创建时的协议配置，兼容细节见[V0.3 验收记录](V0.3-ACCEPTANCE.md)。

### Linux 边界与运行方式

StepX 本地资料显示现有 Linux/chroot 与 Android 宿主共享网络命名空间，OpenClaw Gateway 使用 18789，宿主服务使用 18790 和 18791；端口及认证配置属于 vendor 约束。资料未提供受支持的独立 App 启动 `openclaw_env` 或进入 chroot 的接口。因此 APK 不会唤醒或保活该容器，也不越过 Android sandbox，不调用 root、ADB、chroot 或 vendor 私有 Activity。

日常使用无需通过 Amoo 助手聊天：用户打开 DAXQO 后，App 连接 Linux 侧 DAXQO runtime-control bridge；bridge 在已经运行的 Linux 环境中启动 DAXQO 自己的 supervisor 并管理 backend。模型 key 保留在 Linux 端。如果 StepX Linux 容器没有运行，App 只能报告离线，并等待用户通过设备原有方式启动它。Android 前台 service、通知或轮询都不能保证 vendor 容器保活。定时任务同样只在 DAXQO Linux 服务在线时检查。

运行桥只绑定 `127.0.0.1:18882`：

| 方法 | 路径 | 请求 | 说明 |
| --- | --- | --- | --- |
| GET | `/healthz` | 无认证 | Bridge 健康检查 |
| GET | `/api/runtime/status` | `Authorization: Bearer <local capability>` | 返回 `{state,backend:{connected,pid?},app:{name,version}}` |
| POST | `/api/runtime/start` | Bearer capability，JSON `{}` | 启动或确认 backend；返回 202/200 |
| POST | `/api/runtime/restart` | Bearer capability，JSON `{}` | 有活动任务时返回 409 |
| POST | `/api/runtime/update` | Bearer capability，版本与 SHA-256 文件清单 | 空闲时校验、备份、测试并更新；失败时恢复旧代码 |

Bridge 和 `18880` GUI 使用同一 local capability；它不是模型凭据。Bridge 是 DAXQO backend 控制面，不是 vendor 容器管理接口。

### 构建

Windows PC 若没有 Android 构建工具，可在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -InstallTools
```

脚本下载并校验 Eclipse Temurin JDK 17、Gradle 8.10.2 和 Google 官方 Android command-line tools，然后安装 `platform-tools`、Android 35 platform 与 build-tools 35.0.0 并构建 debug APK。工具与 SDK 放在已 gitignore 的 `.tools/`；APK 输出在 `android/app/build/outputs/apk/debug/app-debug.apk`。已有工具时可省略 `-InstallTools`；脚本会自动补齐缺项。首次构建需要网络访问官方发行站点并接受 Android SDK licenses。

构建配置：Android Gradle Plugin 8.7.3、Gradle 8.10.2、compile SDK 35、target SDK 35、min SDK 26。App 强制浅色主题、浅色系统栏和深色图标，并关闭 WebView 自动反色。Android 15 edge-to-edge system bar 与 IME insets 由根布局处理；Activity 使用 `adjustResize` 让键盘避开 composer。V0.3 版本为 `0.3.0`（versionCode 3），启动图标使用原 Logo 左侧独立箭头标识。仓库内是开发构建配置；本地 APK 使用 debug 签名，正式发布签名由维护者管理。构建脚本不会安装 APK、启动 ADB 或操作手机。

构建工具官方来源：[Android command-line tools](https://developer.android.com/studio#command-line-tools-only)、[Gradle distributions](https://services.gradle.org/distributions/)、[Eclipse Temurin API](https://api.adoptium.net/v3/assets/latest/17/hotspot?architecture=x64&image_type=jdk&os=windows&vendor=eclipse)。安全设置参考 [Android WebView](https://developer.android.com/develop/ui/views/layout/webapps/webview) 和 [cross-app scripting risks](https://developer.android.com/privacy-and-security/risks/cross-app-scripting)。

---

## English

### Android app

`android/` is a native Java Android app with package name `io.daxqo.app` and display name “达序 DAXQO”. It has no third-party UI dependency. The main screen loads the fixed URL `http://127.0.0.1:18880/` in a WebView. The WebView accepts only this HTTP loopback host and port; other top-level pages are handed to the system browser, and external subresources are blocked. External links are rejected if they contain user-info, are too long, or contain credential parameters after normalization.

The app does not expose a general-purpose JavaScript-to-native execution bridge. Its only narrow file API is `window.DAXQOFiles.downloadFile(relativePath)`. It is available only to the trusted loopback GUI top-level page, accepts relative paths only, and always requests `/api/files/download`. It never exposes the capability to the page and does not support arbitrary URLs, file paths, or command execution. File access is disabled, mixed content is disabled, and Safe Browsing is enabled. HTTP clients do not follow redirects, preventing a capability from being forwarded away from loopback.

On Android 10 and newer, downloads are saved under `Downloads/DAXQO/` through MediaStore, with a 20 MiB per-file limit. Android 8/9 falls back to the app-specific Downloads directory and requests no broad storage permission. The GUI can use the narrow API for `blob:` downloads unsupported by WebView. Web uploads use Android's system document picker, allow multiple selections, and receive temporary read access only to the selected URIs. No broad storage permission is requested. Each message can include up to 6 attachments, each up to 20 MiB, uploaded to the local Linux workspace. Image understanding depends on the selected model and runtime; successful import does not establish that vision works.

Linux can generate a one-time pairing link in the form `daxqo://connect#token=<capability>`. Android accepts only this scheme and host, validates token length and characters, and stores it in app-private SharedPreferences along with its SHA-256 fingerprint. Reimporting the same link into the same installation is rejected. The token is then placed in the GUI URL fragment as `#token=…`. The fragment is not sent in the initial HTTP request; the GUI reads it for existing API authentication and then removes it from the address. The capability is not a model API key, and no fixed capability is embedded in the APK, source, logs, or external URLs. A new pairing link replaces the saved token. Clearing app data or reinstalling clears the local one-time record, so pair the new installation again from Linux.

The native connection panel appears only while checking the connection or after a failure; it is hidden when the GUI is online. On a paired cold start, the app checks the runtime-control bridge first. If the backend is disconnected, the app requests startup and polls for up to 20 seconds, loading the GUI only after readiness is confirmed. The panel supports start/retry and restart; the bridge rejects restart with HTTP 409 while a task is active. If the bridge cannot be reached, the app reports that the Linux environment is offline. The WebView top-level trust state is maintained on the UI thread; the download JavaScript interface reads that state without accessing WebView from the Java Bridge thread.

V0.3 defaults to 2 concurrent tasks (configurable from 1 to 4), with at most 2 Codex subagents at depth 1. Public progress shows phases, summaries, and run status; hidden chain-of-thought is not exposed. Default automatic compaction threshold is 32000 tokens and the context window is 65536 tokens. A controlled check with temporary 8000/16000 settings was completed and defaults were restored; long-duration and high-load testing remains incomplete. One acceptance run found lost subagent messages with catalog v2. A new session using catalog v1 with search disabled passed. Existing sessions retain the protocol configuration they were created with; see the [V0.3 acceptance record](V0.3-ACCEPTANCE.md) for compatibility details.

### Linux boundaries and operation

Local StepX documentation indicates that its Linux/chroot environment shares a network namespace with the Android host. The OpenClaw Gateway uses port 18789, and host services use 18790 and 18791; these ports and authentication settings are vendor constraints. The documentation provides no supported interface for a standalone app to start `openclaw_env` or enter the chroot. Therefore, the APK cannot wake or keep that container alive and does not bypass the Android sandbox or call root, ADB, chroot, or vendor-private activities.

Daily use does not require Amoo assistant chat: the user opens DAXQO, which connects to the Linux-side DAXQO runtime-control bridge. The bridge starts DAXQO's own supervisor and manages its backend inside a Linux environment that is already running. Model keys remain on Linux. If StepX's Linux container is not running, the app can only report offline and wait for the user to start it through the device's supported flow. An Android foreground service, notification, or polling cannot guarantee that the vendor container stays alive. Scheduled tasks are likewise checked only while the DAXQO Linux service is online.

The runtime-control bridge binds only to `127.0.0.1:18882`:

| Method | Path | Request | Description |
| --- | --- | --- | --- |
| GET | `/healthz` | No authentication | Bridge health check |
| GET | `/api/runtime/status` | `Authorization: Bearer <local capability>` | Returns `{state,backend:{connected,pid?},app:{name,version}}` |
| POST | `/api/runtime/start` | Bearer capability, JSON `{}` | Starts or confirms the backend; returns 202/200 |
| POST | `/api/runtime/restart` | Bearer capability, JSON `{}` | Returns 409 while a task is active |
| POST | `/api/runtime/update` | Bearer capability, version and SHA-256 file manifest | Verifies, backs up, tests, and updates while idle; restores old code on failure |

The bridge and GUI on port `18880` use the same local capability. It is not a model credential. The bridge controls the DAXQO backend; it is not an interface for managing the vendor container.

### Build

On Windows, if Android build tools are not installed, run this from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -InstallTools
```

The script downloads and verifies Eclipse Temurin JDK 17, Gradle 8.10.2, and Google's official Android command-line tools. It then installs `platform-tools`, Android 35 platform, and build-tools 35.0.0 before building a debug APK. Tools and SDK files go under the gitignored `.tools/`; the APK is written to `android/app/build/outputs/apk/debug/app-debug.apk`. Omit `-InstallTools` when tools are already available; the script still fills in missing components. The first build requires network access to official distribution sites and acceptance of Android SDK licenses.

Build configuration: Android Gradle Plugin 8.7.3, Gradle 8.10.2, compile SDK 35, target SDK 35, and min SDK 26. The app forces a light theme with light system bars and dark icons, and disables WebView automatic inversion. The root layout handles Android 15 edge-to-edge system bars and IME insets; the Activity uses `adjustResize` to keep the composer clear of the keyboard. V0.3 is version `0.3.0` (versionCode 3), with the standalone left-arrow mark from the original logo as its launcher icon. The repository contains development build configuration; the local APK uses a debug signature, and maintainers manage release signing separately. The build script does not install the APK, start ADB, or operate a phone.

Official build sources: [Android command-line tools](https://developer.android.com/studio#command-line-tools-only), [Gradle distributions](https://services.gradle.org/distributions/), and [Eclipse Temurin API](https://api.adoptium.net/v3/assets/latest/17/hotspot?architecture=x64&image_type=jdk&os=windows&vendor=eclipse). Security settings follow [Android WebView](https://developer.android.com/develop/ui/views/layout/webapps/webview) and [cross-app scripting risk guidance](https://developer.android.com/privacy-and-security/risks/cross-app-scripting).
