# DAXQO for XM

## 中文

DAXQO for XM 在 Termux 的私有 HOME 中运行独立的 Debian PRoot 容器 `daxqo-xm`。不需要 root；服务和配对数据保存在 Termux 私有目录。它使用 Debian、Python venv、固定 Codex runtime 0.163.0 和邮件依赖。不要把仓库放在 `/sdcard` 等共享存储中。

### 安装与运行

1. 从 [Termux 官方安装说明](https://github.com/termux/termux-app#installation)安装 Termux。使用支持的官方来源版本；不要混装不同签名来源的 Termux 插件。
2. 在 Termux 中取得包含 XM 支持的 DAXQO 源码，并确认 checkout 位于 Termux 私有 HOME 下。进入仓库后安装：

   ```sh
   bash scripts/install_xm.sh install
   bash scripts/install_xm.sh doctor
   ```

   安装器创建独立 Debian 容器、配置与 Termux 用户匹配的 `daxqo` 用户，并在 `~/.local/share/daxqo` 安装 backend、Python venv、Codex runtime 与邮件依赖。`doctor` 输出环境检查结果，不显示 API key 或配对 capability。

   安装器先更新 Termux 整套基础包，避免旧 bootstrap 的 curl/OpenSSL ABI 冲突。PRoot-Distro v5 使用 Termux 官方 Debian trixie 系统包，并验证固定 SHA-256；不依赖 Docker Hub。若手机下载 GitHub 较慢，可在电脑下载[官方 ARM64 系统包](https://github.com/termux/proot-distro/releases/download/v4.29.0/debian-trixie-aarch64-pd-v4.29.0.tar.xz)，传入 Termux 私有 HOME 后设置 `DAXQO_XM_ROOTFS=/绝对路径/系统包.tar.xz` 再执行安装；安装器仍执行同一哈希校验。

   Debian 的 glibc 需要独立 DNS 配置。安装器仅在该容器内设置 [AliDNS](https://help.aliyun.com/en/dns/public-dns-free-version-access-speed-limit-notification-dns) `223.5.5.5 / 223.6.6.6`，可用 `DAXQO_XM_DNS="服务器IP1 服务器IP2"` 在安装时覆盖；Android 全局 DNS 不受影响。

3. 使用 XM 构建的 Android APK（包名 `io.daxqo.xm`）。Windows 构建命令：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\android\build.ps1 -Xm -Lint
   ```

   XM 配对链接使用 `daxqo-xm://connect#token=…`。在 Termux 私有 checkout 中执行 `bash scripts/install_xm.sh pair` 会显示链接；将它视为秘密，只导入自己的 DAXQO for XM。
4. 在 DAXQO for XM 连接区点“打开 Termux”，或手动切换到 Termux，启动服务：

   ```sh
   daxqo-xm start
   ```

   保持该前台会话运行；可以切换到 DAXQO App。停止服务时在 Termux 中按 Ctrl+C。`daxqo-xm doctor` 检查环境，`daxqo-xm status` 检查后台、进程归属和 Codex 连接，`daxqo-xm pair` 重新显示配对链接，`daxqo-xm test` 运行 guest 单元测试。模型 API key 需在 XM 设置中另行填写，环境安装器不复制其他应用的密钥。

### 小米系统设置与边界

- 无人值守运行需手动将 DAXQO 与 Termux 的“应用信息 → 省电策略”设为“无限制”；在 DAXQO 的“权限管理 → 其他权限”中允许“锁屏显示”和“后台弹出界面”。Termux 还应按设备选项允许自启动。菜单名称会因澎湃 OS 版本和地区变化；可参考[小米 HyperOS 开发者文档](https://dev.mi.com/xiaomihyperos/documentation/detail?pId=1624)。控制页“4 检查澎湃OS权限”只打开 DAXQO 应用信息，不会自动更改授权。
- 本次真机原有 MIUIOP 的 `10020/10021` 规则为 `ignore`，在 GUI 补齐后变为 `allow`；DAXQO 省电策略原为“智能”，经 GUI 设为“无限制”后，原生 `batteryOptimizationsIgnored` 也变为 `true`。Termux 同样设为“无限制”。安装脚本不会静默授予这些权限；本次按机主环境补全请求，在完整授权下通过系统设置界面配置。
- 如果 Android 提示受限设置，机主需从 DAXQO for XM 的系统应用信息页手动允许，再按需启用辅助功能。无人值守安装器不会代替系统授权；已有机主完整控制授权及本轮环境配置请求，只用于机主明确要求的本次测试。
- PRoot Debian 使用 Termux 的应用 UID 运行，不是完整 Linux 虚拟机，也不提供 init/systemd。不要同时启动原版 DAXQO Linux 服务：两者使用相同的 loopback 端口 `18880`、`18882`。
- `start` 持有 Termux wake lock，并以前台 Termux 会话承载服务。可以切换应用，但需保持 Termux 会话中的进程运行。HyperOS 仍可能因省电、内存回收、重启或用户强制停止而终止它；不保证息屏长跑或 24 小时保活。
- Termux 与 PRoot 的范围及限制见 [Termux](https://github.com/termux/termux-app#installation) 和 [proot-distro](https://github.com/termux/proot-distro)。仅安装官方 Termux；Android App 只打开 Termux，不通过权限桥执行命令。

### 当前验收状态

2026-10-23，在 Xiaomi 2410DPN6CC、HyperOS `OS3.0.309.0.WOBCNXM`、Android 16、ARM64、WebView 155 上完成：

- 已安装并配对独立 XM APK（`io.daxqo.xm`）、官方 Termux 0.118.3、PRoot-Distro 5.10.2、Debian trixie、Python 3.13 venv、Node 20 / npm、Codex 0.163.0、dkimpy / dnspython、证书、Git / ripgrep 与 DNS。
- 环境自检 15 项通过；手机本机 backend、运行桥和 Codex App Server 连接正常，WebView 显示“已连接”；强制停止并冷启动 XM 后仍能连接。已为本机 Termux 配置省电策略“无限制”。
- 首轮曾检查 30 秒后的本机连接及 DeepSeek HTTPS，但用户随后确认期间曾亲自解锁。因此撤回其“连续锁屏/息屏通过”的结论，只保留当时连接可用的事实。
- 原有 153 项后端测试在手机上全部通过，另新增 1 项 Android 内核无 `task/*/children` 时的进程发现回归通过。修复前曾复现 detached 子进程回收失败；修复保留 UID、PID 启动时间和发信号前身份验证。
- XM 构建与 Lint 通过；Android passive callbacks 2000 次、审批/唤醒 292 断言与 64 并发重试通过。WSL 原有 153 项为 137 passed、16 skipped，新增内核兼容回归与 3 项安装/配对测试通过。
- 后续用户已配置模型 API key 并启用完整手机控制授权。DeepSeek Flash API、真实 Python 工具执行、无密码息屏定时任务和原生自动唤醒均已通过；唤醒结论适用于完成上述澎湃 OS 环境配置后的实测，不代表未配置设备也可无条件唤醒。文件随机标识、SHA-256、ARM64 架构及系统锁屏状态已独立核对。用户已删除密码，因此不能作为密码锁屏结果。时间与证据见[2026-10-23 真机复测](XM-ACCEPTANCE.md)。邮件账户闭环与长时间息屏任务尚未验收。

## English

DAXQO for XM runs in a separate Debian PRoot container named `daxqo-xm`, with its checkout and app data kept under Termux's private HOME. It needs no root access. See the steps above for installation, pairing, and startup. Build the Android app with `android/build.ps1 -Xm -Lint`; the XM package is `io.daxqo.xm` and its pairing scheme is `daxqo-xm`.

Keep the Termux foreground session running after `daxqo-xm start`. On the tested HyperOS device, set both DAXQO and Termux battery policies to Unrestricted, and allow DAXQO's “Show on Lock screen” and “Display pop-up windows while running in the background” permissions in Other permissions. These settings were configured manually; the app does not grant them. Do not run the original DAXQO Linux service alongside XM because they share local ports. Installation, pairing, local Codex initialization, cold app launch and device unit tests passed. The first 30-second lock test was invalidated by the user's manual unlock. The user subsequently configured an API key and removed the lock credential. API verification, a real Python/file task, a scheduled task while screen-off/locked and native wake/dismiss of a non-secure swipe lock passed after the required system settings were configured. See the [device retest](XM-ACCEPTANCE.md). Secure credential locks and long screen-off runs remain unverified.
