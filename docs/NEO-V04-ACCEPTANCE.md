# STEPX Neo V0.4 验收 / STEPX Neo V0.4 acceptance

2026-10-24 的最新输入与执行效率验收见 [V0.4 指南](V0.4.md)及[中文输入说明](TEXT-INPUT.md)。下文保留 2026-10-23 的历史构建与验收结果。

2026-10-23，STEPX Neo PZ261A（Android 16 / SDK 36）完成 Standard V0.4 真机回归。Standard APK 安装身份为 `io.daxqo.app`、版本 `0.4.0`、`platformId=standard`；设备安装后实际 APK SHA-256 为 `cdcc9180acfec0c5ceabac7e89b4caf9d8c47b90a687a5caf992452356a1cea5`。应用商店候选遗漏应用宝 `com.tencent.android.qqdownloader` 的问题已修复。

手机 Linux 通过既有安全维护刷新运行桥，并按带哈希的 manifest 更新源码。166 项后端测试全部通过（28.996 秒）；模型配置摘要、配对资料和既有任务 ID 均保留。原始可读验收记录 `dist/neo/native-acceptance.json` 的 11 项均通过且 `errors=[]`。它覆盖 HTTP 401/403、Settings/通知/Home 被动切换、非安全息屏锁唤醒、重放与冲突保护、计算器启动、陈旧快照拒绝及新节点操作、截图（1200×2670）。该文件中的 `expectedApkSha256` 是期望值；已安装 APK 的实际哈希由 `dist/neo/installed-final.json` 单独记录。

真实模型任务 `ebf83339-e0c0-4545-8752-e37f5b4a288e` 使用 `deepseek-flash`，从 `2026-10-23T12:21:43Z` 到 `12:22:12Z`，耗时 29 秒。任务在息屏、非安全锁状态开始；清除 PC 转发后由手机独立唤醒，完成计算器 `8×7=56`，并截图核验结果。任务共调用 13 次 Android 工具及 1 次终端写入验收 JSON。独立目视复核 `dist/neo/model-calculator-independent.png` 确认屏幕显示表达式和结果 56；任务与 UI 记录分别见 `dist/neo/model-task-final.json`、`dist/neo/model-ui-result.json`。

控制模式沿用用户原有 `full` 设置，没有提高权限；FileManager 临时安装来源设置已恢复为 deny。Standard 与 XM 当前补丁均已构建、通过 Lint 和 APK profile 检查。此轮没有重新验收邮件闭环、安全密码锁、SIM 限制、长期息屏、电池行为、所有安装流程或支付操作；XM 新 Linux 路径仍未真机端到端验收。其历史试验不构成当前路径证明。原始 `dist/neo` 文件为本地验收证据，不随本文加入版本控制。

## English

On 2026-10-23, Standard V0.4 completed device regression on STEPX Neo PZ261A (Android 16 / SDK 36). The installed app identity was `io.daxqo.app`, version `0.4.0`, `platformId=standard`; the actual installed APK SHA-256 was `cdcc9180acfec0c5ceabac7e89b4caf9d8c47b90a687a5caf992452356a1cea5`. The missing Tencent MyApp candidate (`com.tencent.android.qqdownloader`) was fixed.

The existing phone Linux environment received a runtime-bridge refresh and manifest update through its established maintenance path. All 166 backend tests passed in 28.996 seconds; configuration summary, pairing data, and existing task IDs were preserved. The readable `dist/neo/native-acceptance.json` reports 11 passed checks and `errors=[]`, covering HTTP 401/403, passive Settings/notification/Home transitions, wake from a non-secure screen-off lock, replay/conflict protection, calculator launch, stale-snapshot rejection and fresh-node actions, and a 1200×2670 screenshot. Its `expectedApkSha256` is an expected value; the actual installed APK hash is recorded separately in `dist/neo/installed-final.json`.

Real model task `ebf83339-e0c0-4545-8752-e37f5b4a288e` used `deepseek-flash` from `2026-10-23T12:21:43Z` to `12:22:12Z` (29 seconds). Starting screen-off with a non-secure lock, it independently woke the phone after PC forwarding was removed, entered `8×7` in Calculator, and verified `56` in a screenshot. It used 13 Android tool calls and one terminal write for the acceptance JSON. Independent visual review of `dist/neo/model-calculator-independent.png` confirmed the expression and result. Task and UI records are `dist/neo/model-task-final.json` and `dist/neo/model-ui-result.json`.

The user's existing `full` control mode was retained without raising permissions; FileManager's temporary install-source setting was restored to deny. Both Standard and XM patches passed build, lint, and APK profile checks. This run did not retest email closure, secure-password lock, SIM restrictions, long screen-off operation, battery behavior, all installation flows, or payment actions. The current XM Linux path still lacks on-device end-to-end acceptance. Raw `dist/neo` evidence remains local and is not included with this document.
