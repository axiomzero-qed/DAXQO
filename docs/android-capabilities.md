# Android capabilities research / Android 能力研究

## Scope / 范围

This note summarizes a read-only review of the extracted Android-abilities documentation matching APK version `100007`, plus the device facts supplied for the STEPX handset. It does not reproduce vendor documentation, credentials, internal service details, or user data. The APK and extracted materials must not be committed to this repository.

本文概述对与 APK 版本 `100007` 匹配的 Android abilities 文档及 STEPX 设备已提供信息的只读审查结果。不复制厂商文档正文、凭据、内部服务细节或用户数据。APK 与提取材料不得提交到本仓库。

## Findings / 发现

The documented call path is `ability.list` to discover a method, then `ability.call` with the method and parameter object. The host is the vendor ClawX privileged app (`com.stepos.stepclaw`), observed on this device under `/system_ext/priv-app/ClawX`, shared system UID 1000. It holds system/privileged access including `ACCESS_AGENT_SERVICE`, `ACCESS_MCP_SERVICES`, and settings-write access; the ordinary DAXQO APK does not inherit those grants.

文档中的调用路径是先用 `ability.list` 发现方法，再通过 `ability.call` 传入方法名和参数对象。宿主是厂商 ClawX 特权应用（`com.stepos.stepclaw`）；本设备上观察到它位于 `/system_ext/priv-app/ClawX`，共享 system UID 1000，并持有 `ACCESS_AGENT_SERVICE`、`ACCESS_MCP_SERVICES` 和系统设置写入等系统访问能力。普通 DAXQO APK 不会继承这些授权。

Device check on 2026-10-22: a DAXQO Linux task used the existing configured vendor CLI, without model-driven vendor-assistant planning, to call `settings_get_auto_brightness_state`; the connected AgentBus node returned success and “automatic brightness is enabled.” No setting was changed. The live node advertised `ability.call` only; the documented `ability.list` discovery route has not been verified on it.

2026-10-22 真机检查：DAXQO 的 Linux 任务通过已有、已配置的厂商 CLI 调用 `settings_get_auto_brightness_state`，连接中的 AgentBus 节点成功返回“自动亮度已开启”；没有让厂商助手规划任务，也未修改设置。当前节点只声明支持 `ability.call`，文档里的 `ability.list` 发现路径尚未在该节点验证。

Abilities include app-specific and system-setting operations, such as opening an app by name/package, changing selected settings, querying some device/app state, and opening settings pages. These native operations can avoid UI simulation for the specific supported action; they do not grant DAXQO the vendor host's privileged access.

Abilities 包含面向特定应用和系统设置的操作，例如按名称/包名打开应用、调整部分设置、查询部分设备/应用状态，以及打开设置页面。对已支持的具体动作，这类原生操作可以避免模拟界面；但它不会让 DAXQO 获得厂商宿主的特权访问。

`gui_agent` is documented as a natural-language delegation to a GUI agent for multi-step interaction in a named app. Its parameters are `app_name` and `task`, and it is routed through the vendor CommonAbility proxy. It is a screen-interaction workflow, not a general semantic API for arbitrary off-screen operation. Whether it uses a cloud service, which model it uses, and whether it can work offline have not been verified.

文档中的 `gui_agent` 是把自然语言任务委托给 GUI agent，在指定应用中执行多步界面交互。参数为 `app_name` 和 `task`，调用经由厂商 CommonAbility proxy。它属于屏幕交互流程，不是可对任意应用进行离屏操作的通用语义 API。它是否使用云服务、使用哪种模型、能否离线工作，均未验证。

The reviewed abilities do not document a device wake/unlock API or a flow for granting DAXQO Accessibility or vendor signature permissions. Opening lock-screen settings and changing screen timeout are not unlock operations. No ability can be assumed to bypass a secure lock screen or Android's permission controls.

已查阅的 abilities 未记载设备唤醒/解锁 API，也未记载为 DAXQO 授予无障碍权限或厂商签名权限的流程。打开锁屏设置、调整息屏时间都不等于解锁。不得假定任何 ability 可以绕过安全锁屏或 Android 权限控制。

## DAXQO direction / DAXQO 方向

Keep the general DAXQO foundation native and permission-scoped. A future StepOS integration should be optional, capability-detected, and able to fall back when unavailable. It must not assume or attempt to acquire vendor signature permissions. For each specific app and permission, require the user's explicit consent; prefer documented official or vendor authorization APIs, and only then consider operations allowed by the system. This is a design direction, not an implemented permission broker.

DAXQO 通用底座继续采用原生实现并按权限范围工作。未来的 StepOS 适配应为可选能力，先检测是否可用，并在不可用时回退；不得假定或尝试获取厂商签名权限。针对具体应用和具体权限，应先取得用户明确同意，优先使用有文档依据的官方或厂商授权 API，再考虑系统允许的操作。这是设计方向，不代表权限代理已实现。

Runtime permissions, Accessibility access, device-admin privileges, and signature permissions are distinct. Do not request or grant them all indiscriminately. Do not claim that PIN, biometrics, system-signature permissions, or `FLAG_SECURE` protections can be bypassed.

运行时权限、无障碍访问、设备管理员权限和签名权限彼此不同。不得不加区分地请求或授予全部权限。不得声称可以绕过 PIN、生物识别、系统签名权限要求或 `FLAG_SECURE` 保护。

## Keyguard status / 锁屏状态

Android's official [`KeyguardManager.requestDismissKeyguard()`](https://developer.android.com/reference/android/app/KeyguardManager#requestDismissKeyguard(android.app.Activity,android.app.KeyguardManager.KeyguardDismissCallback)) is a request, not a bypass. With owner authorization, the developer enabled DAXQO's “Display views on lock screen” switch through normal system UI; on STEPX/Android 16 appops reports `STEP_ACTIVITY_ON_LOCKED_SCREEN: allow` for `stepos.permission.ACTIVITY_ON_LOCKED_SCREEN` (`normal|appop`, not signature-level). Native acceptance passed at 01:06 China Standard Time on 2026-10-23: screen-off+locked wake resulted in screen-on and unlocked after 1.359 seconds; screen-on+locked dismissal succeeded after 1.265 seconds, with SDK callback `dismiss_succeeded`. Control remained enabled with empty `needsUser`. At 01:07:26–01:08:35, an autonomous local Harness task using `deepseek-flash` completed in about 69 seconds with 15 Android tool calls: from screen-off+locked, the phone Agent woke/unlocked through DAXQO MCP, opened Calculator, entered `6+7=`, and verified `13` with screenshots/UI tree. USB debugging forwards were removed during the run and the developer did not touch or direct the phone. A separate read-only ADB retrieval afterward produced a 1200×2670 PNG independently confirming `6+7` and `13`. This local Harness loop passed; the separate email loop was not retested. Candidate lint was 0 errors / 16 warnings; 64 gate combinations, 2,000 callback host checks, and 131 phone-backend checks passed. Secure hardware/biometric cases, other devices, and battery-optimization behavior remain untested; an optional vendor adapter or permission broker is not implemented.

Android 官方 [`KeyguardManager.requestDismissKeyguard()`](https://developer.android.com/reference/android/app/KeyguardManager#requestDismissKeyguard(android.app.Activity,android.app.KeyguardManager.KeyguardDismissCallback)) 是请求，不是绕过锁屏。经用户授权，开发者通过正常系统 GUI 开启 DAXQO 的“锁屏显示”；STEPX/Android 16 的 appops 对 `stepos.permission.ACTIVITY_ON_LOCKED_SCREEN` 返回 `allow`（权限级别 `normal|appop`，不是签名权限）。2026-10-23 北京时间 01:06 原生验收通过：息屏且锁定时，1.359 秒后亮屏并解锁；亮屏且锁定时，1.265 秒后解锁，SDK 回调 `dismiss_succeeded`。控制保持开启且 `needsUser` 为空。随后 01:07:26–01:08:35，`deepseek-flash` 自主 Harness 本机任务约 69 秒完成、调用 15 次 Android 工具：手机 Agent 从息屏锁定状态经 DAXQO MCP 唤醒/解锁、打开计算器、输入 `6+7=` 并通过截图/UI 树核验 `13`。执行期间 USB 调试转发已撤除，开发者未触摸或指挥；完成后 ADB 只读取得的 1200×2670 PNG 也独立显示 `6+7` 和 `13`。本机 Harness 闭环通过；邮件闭环未在本次重测。候选 lint 为 0 errors / 16 warnings；64 种门槛组合、2000 次回调 host 检查和手机 backend 131 项检查通过。安全硬件/生物识别、其他机型及电池优化行为未测；可选厂商适配或权限代理尚未实现。

## Evidence and limits / 证据与限制

The reviewed notes are `assets_openclaw_skills_android-abilities_SKILL.md` (call format), `..._app_device.md` (app operations), `..._gui_agent.md` (GUI-agent method and parameters), `..._system_settings.md` (settings and lock-screen pages), and `..._session_runtime.md` (cloud-side versus endpoint tools), under the local vendor-audit extraction. Only the explicitly recorded read-only CLI call has passed live integration. A general capability adapter, permission broker, off-screen GUI control, and vendor unlock behavior remain unverified.

查阅材料为本地 vendor-audit 提取目录中的 `assets_openclaw_skills_android-abilities_SKILL.md`（调用格式）、`..._app_device.md`（应用操作）、`..._gui_agent.md`（GUI-agent 方法与参数）、`..._system_settings.md`（系统设置与锁屏页面）和 `..._session_runtime.md`（云侧与端侧工具）。只有明确记录的只读 CLI 调用通过了真机集成检查；通用能力适配、权限代理、离屏 GUI 控制和厂商解锁行为仍未验证。
